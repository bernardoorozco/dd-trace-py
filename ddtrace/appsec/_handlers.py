from collections.abc import MutableMapping
import functools
import io
import json

from wrapt import when_imported
from wrapt import wrap_function_wrapper as _w
import xmltodict

from ddtrace.appsec._asm_request_context import asm_context
from ddtrace.appsec._constants import SPAN_DATA_NAMES
from ddtrace.appsec._iast._patch import if_iast_taint_returned_object_for
from ddtrace.appsec._iast._patch import if_iast_taint_yield_tuple_for
from ddtrace.appsec._iast._utils import _is_iast_enabled
from ddtrace.contrib import trace_utils
from ddtrace.contrib.trace_utils import _get_request_header_user_agent
from ddtrace.contrib.trace_utils import _set_url_tag
from ddtrace.ext import SpanTypes
from ddtrace.ext import http
from ddtrace.internal import core
from ddtrace.internal.constants import HTTP_REQUEST_BLOCKED
from ddtrace.internal.constants import RESPONSE_HEADERS
from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils import http as http_utils
from ddtrace.internal.utils.http import parse_form_multipart
from ddtrace.settings.asm import config as asm_config


MessageMapContainer = None
try:
    from google._upb._message import MessageMapContainer  # type: ignore[no-redef]
except ImportError:
    pass


log = get_logger(__name__)
_BODY_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def _get_content_length(environ):
    content_length = environ.get("CONTENT_LENGTH")
    transfer_encoding = environ.get("HTTP_TRANSFER_ENCODING")

    if transfer_encoding == "chunked" or content_length is None:
        return None

    try:
        return max(0, int(content_length))
    except Exception:
        return 0


# set_http_meta


def _on_set_http_meta(
    span,
    request_ip,
    raw_uri,
    route,
    method,
    request_headers,
    request_cookies,
    parsed_query,
    request_path_params,
    request_body,
    status_code,
    response_headers,
    response_cookies,
):
    if _is_iast_enabled():
        from ddtrace.appsec._iast.taint_sinks.insecure_cookie import asm_check_cookies

        if response_cookies:
            asm_check_cookies(response_cookies)

    if asm_config._asm_enabled and span.span_type == SpanTypes.WEB:
        status_code = str(status_code) if status_code is not None else None

        addresses = [
            (SPAN_DATA_NAMES.REQUEST_HTTP_IP, request_ip),
            (SPAN_DATA_NAMES.REQUEST_URI_RAW, raw_uri),
            (SPAN_DATA_NAMES.REQUEST_ROUTE, route),
            (SPAN_DATA_NAMES.REQUEST_METHOD, method),
            (SPAN_DATA_NAMES.REQUEST_HEADERS_NO_COOKIES, request_headers),
            (SPAN_DATA_NAMES.REQUEST_COOKIES, request_cookies),
            (SPAN_DATA_NAMES.REQUEST_QUERY, parsed_query),
            (SPAN_DATA_NAMES.REQUEST_PATH_PARAMS, request_path_params),
            (SPAN_DATA_NAMES.REQUEST_BODY, request_body),
            (SPAN_DATA_NAMES.RESPONSE_STATUS, status_code),
            (SPAN_DATA_NAMES.RESPONSE_HEADERS_NO_COOKIES, response_headers),
        ]
        for k, v in addresses:
            if v is not None:
                asm_context.set_waf_address(k, v)


# ASGI


async def _on_asgi_request_parse_body(receive, headers):
    if asm_config._asm_enabled:
        try:
            data_received = await receive()
        except Exception:
            return receive, None

        async def receive_wrapped(once=[True]):
            if once[0]:
                once[0] = False
                return data_received
            return await receive()

        try:
            body = data_received.get("body", b"")
            content_type = headers.get("content-type") or headers.get("Content-Type")
            if content_type in ("application/json", "text/json"):
                if body is None or body == b"":
                    req_body = None
                else:
                    req_body = json.loads(body.decode())
            elif content_type in ("application/xml", "text/xml"):
                req_body = xmltodict.parse(body)
            elif content_type == "text/plain":
                req_body = None
            else:
                req_body = parse_form_multipart(body.decode(), headers) or None
            return receive_wrapped, req_body
        except Exception:
            return receive_wrapped, None

    return receive, None


# FLASK


def _on_request_span_modifier(
    ctx, flask_config, request, environ, _HAS_JSON_MIXIN, flask_version, flask_version_str, exception_type
):
    req_body = None
    if asm_config._asm_enabled and request.method in _BODY_METHODS:
        content_type = request.content_type
        wsgi_input = environ.get("wsgi.input", "")

        # Copy wsgi input if not seekable
        if wsgi_input:
            try:
                seekable = wsgi_input.seekable()
            # expect AttributeError in normal error cases
            except Exception:
                seekable = False
            if not seekable:
                # https://gist.github.com/mitsuhiko/5721547
                # Provide wsgi.input as an end-of-file terminated stream.
                # In that case wsgi.input_terminated is set to True
                # and an app is required to read to the end of the file and disregard CONTENT_LENGTH for reading.
                if environ.get("wsgi.input_terminated"):
                    body = wsgi_input.read()
                else:
                    content_length = _get_content_length(environ)
                    body = wsgi_input.read(content_length) if content_length else b""
                environ["wsgi.input"] = io.BytesIO(body)

        try:
            if content_type in ("application/json", "text/json"):
                if _HAS_JSON_MIXIN and hasattr(request, "json") and request.json:
                    req_body = request.json
                elif request.data is None or request.data == b"":
                    req_body = None
                else:
                    req_body = json.loads(request.data.decode("UTF-8"))
            elif content_type in ("application/xml", "text/xml"):
                req_body = xmltodict.parse(request.get_data())
            elif hasattr(request, "form"):
                req_body = request.form.to_dict()
            else:
                # no raw body
                req_body = None
        except Exception:
            log.debug("Failed to parse request body", exc_info=True)
        finally:
            # Reset wsgi input to the beginning
            if wsgi_input:
                if seekable:
                    wsgi_input.seek(0)
                else:
                    environ["wsgi.input"] = io.BytesIO(body)
    return req_body


def _on_request_init(wrapped, instance, args, kwargs):
    wrapped(*args, **kwargs)
    if _is_iast_enabled():
        try:
            from ddtrace.appsec._iast._taint_tracking import OriginType
            from ddtrace.appsec._iast._taint_tracking import origin_to_str
            from ddtrace.appsec._iast._taint_tracking import taint_pyobject
            from ddtrace.appsec._iast.processor import AppSecIastSpanProcessor

            if not AppSecIastSpanProcessor.is_span_analyzed():
                return

            instance.query_string = taint_pyobject(
                pyobject=instance.query_string,
                source_name=origin_to_str(OriginType.QUERY),
                source_value=instance.query_string,
                source_origin=OriginType.QUERY,
            )
            instance.path = taint_pyobject(
                pyobject=instance.path,
                source_name=origin_to_str(OriginType.PATH),
                source_value=instance.path,
                source_origin=OriginType.PATH,
            )
        except Exception:
            log.debug("Unexpected exception while tainting pyobject", exc_info=True)


def _on_flask_patch(flask_version):
    if _is_iast_enabled():
        from ddtrace.appsec._iast._metrics import _set_metric_iast_instrumented_source
        from ddtrace.appsec._iast._patch import _patched_dictionary
        from ddtrace.appsec._iast._patch import try_wrap_function_wrapper
        from ddtrace.appsec._iast._taint_tracking import OriginType

        try_wrap_function_wrapper(
            "werkzeug.datastructures",
            "Headers.items",
            functools.partial(if_iast_taint_yield_tuple_for, (OriginType.HEADER_NAME, OriginType.HEADER)),
        )
        _set_metric_iast_instrumented_source(OriginType.HEADER_NAME)
        _set_metric_iast_instrumented_source(OriginType.HEADER)

        try_wrap_function_wrapper(
            "werkzeug.datastructures",
            "ImmutableMultiDict.__getitem__",
            functools.partial(if_iast_taint_returned_object_for, OriginType.PARAMETER),
        )
        _set_metric_iast_instrumented_source(OriginType.PARAMETER)

        try_wrap_function_wrapper(
            "werkzeug.datastructures",
            "EnvironHeaders.__getitem__",
            functools.partial(if_iast_taint_returned_object_for, OriginType.HEADER),
        )
        _set_metric_iast_instrumented_source(OriginType.HEADER)

        if flask_version >= (2, 0, 0):
            # instance.query_string: raising an error on werkzeug/_internal.py "AttributeError: read only property"
            try_wrap_function_wrapper("werkzeug.wrappers.request", "Request.__init__", _on_request_init)

        _set_metric_iast_instrumented_source(OriginType.PATH)
        _set_metric_iast_instrumented_source(OriginType.QUERY)

        # Instrumented on _ddtrace.appsec._asm_request_context._on_wrapped_view
        _set_metric_iast_instrumented_source(OriginType.PATH_PARAMETER)

        try_wrap_function_wrapper(
            "werkzeug.wrappers.request",
            "Request.get_data",
            functools.partial(_patched_dictionary, OriginType.BODY, OriginType.BODY),
        )
        try_wrap_function_wrapper(
            "werkzeug.wrappers.request",
            "Request.get_json",
            functools.partial(_patched_dictionary, OriginType.BODY, OriginType.BODY),
        )

        _set_metric_iast_instrumented_source(OriginType.BODY)

        if flask_version < (2, 0, 0):
            _w(
                "werkzeug._internal",
                "_DictAccessorProperty.__get__",
                functools.partial(if_iast_taint_returned_object_for, OriginType.QUERY),
            )
            _set_metric_iast_instrumented_source(OriginType.QUERY)


def _on_django_func_wrapped(fn_args, fn_kwargs, first_arg_expected_type, *_):
    # If IAST is enabled and we're wrapping a Django view call, taint the kwargs (view's
    # path parameters)
    if _is_iast_enabled() and fn_args and isinstance(fn_args[0], first_arg_expected_type):
        from ddtrace.appsec._iast._taint_tracking import OriginType  # noqa: F401
        from ddtrace.appsec._iast._taint_tracking import is_pyobject_tainted
        from ddtrace.appsec._iast._taint_tracking import origin_to_str
        from ddtrace.appsec._iast._taint_tracking import taint_pyobject
        from ddtrace.appsec._iast._taint_utils import taint_structure
        from ddtrace.appsec._iast.processor import AppSecIastSpanProcessor

        if not AppSecIastSpanProcessor.is_span_analyzed():
            return

        http_req = fn_args[0]

        http_req.COOKIES = taint_structure(http_req.COOKIES, OriginType.COOKIE_NAME, OriginType.COOKIE)
        http_req.GET = taint_structure(http_req.GET, OriginType.PARAMETER_NAME, OriginType.PARAMETER)
        http_req.POST = taint_structure(http_req.POST, OriginType.BODY, OriginType.BODY)
        if (
            getattr(http_req, "_body", None) is not None
            and len(getattr(http_req, "_body", None)) > 0
            and not is_pyobject_tainted(getattr(http_req, "_body", None))
        ):
            try:
                http_req._body = taint_pyobject(
                    http_req._body,
                    source_name=origin_to_str(OriginType.BODY),
                    source_value=http_req._body,
                    source_origin=OriginType.BODY,
                )
            except AttributeError:
                log.debug("IAST can't set attribute http_req._body", exc_info=True)
        elif (
            getattr(http_req, "body", None) is not None
            and len(getattr(http_req, "body", None)) > 0
            and not is_pyobject_tainted(getattr(http_req, "body", None))
        ):
            try:
                http_req.body = taint_pyobject(
                    http_req.body,
                    source_name=origin_to_str(OriginType.BODY),
                    source_value=http_req.body,
                    source_origin=OriginType.BODY,
                )
            except AttributeError:
                log.debug("IAST can't set attribute http_req.body", exc_info=True)

        http_req.headers = taint_structure(http_req.headers, OriginType.HEADER_NAME, OriginType.HEADER)
        http_req.path = taint_pyobject(
            http_req.path, source_name="path", source_value=http_req.path, source_origin=OriginType.PATH
        )
        http_req.path_info = taint_pyobject(
            http_req.path_info,
            source_name=origin_to_str(OriginType.PATH),
            source_value=http_req.path,
            source_origin=OriginType.PATH,
        )
        http_req.environ["PATH_INFO"] = taint_pyobject(
            http_req.environ["PATH_INFO"],
            source_name=origin_to_str(OriginType.PATH),
            source_value=http_req.path,
            source_origin=OriginType.PATH,
        )
        http_req.META = taint_structure(http_req.META, OriginType.HEADER_NAME, OriginType.HEADER)
        if fn_kwargs:
            try:
                for k, v in fn_kwargs.items():
                    fn_kwargs[k] = taint_pyobject(
                        v, source_name=k, source_value=v, source_origin=OriginType.PATH_PARAMETER
                    )
            except Exception:
                log.debug("IAST: Unexpected exception while tainting path parameters", exc_info=True)


def _on_wsgi_environ(wrapped, _instance, args, kwargs):
    if _is_iast_enabled():
        if not args:
            return wrapped(*args, **kwargs)

        from ddtrace.appsec._iast._taint_tracking import OriginType  # noqa: F401
        from ddtrace.appsec._iast._taint_utils import taint_structure
        from ddtrace.appsec._iast.processor import AppSecIastSpanProcessor

        if not AppSecIastSpanProcessor.is_span_analyzed():
            return wrapped(*args, **kwargs)

        return wrapped(*((taint_structure(args[0], OriginType.HEADER_NAME, OriginType.HEADER),) + args[1:]), **kwargs)

    return wrapped(*args, **kwargs)


def _on_django_patch():
    if _is_iast_enabled():
        try:
            from ddtrace.appsec._iast._metrics import _set_metric_iast_instrumented_source
            from ddtrace.appsec._iast._taint_tracking import OriginType

            # we instrument those sources on _on_django_func_wrapped
            _set_metric_iast_instrumented_source(OriginType.HEADER_NAME)
            _set_metric_iast_instrumented_source(OriginType.HEADER)
            _set_metric_iast_instrumented_source(OriginType.PATH_PARAMETER)
            _set_metric_iast_instrumented_source(OriginType.PATH)
            _set_metric_iast_instrumented_source(OriginType.COOKIE)
            _set_metric_iast_instrumented_source(OriginType.COOKIE_NAME)
            _set_metric_iast_instrumented_source(OriginType.PARAMETER)
            _set_metric_iast_instrumented_source(OriginType.PARAMETER_NAME)
            _set_metric_iast_instrumented_source(OriginType.BODY)
            when_imported("django.http.request")(
                lambda m: trace_utils.wrap(
                    m,
                    "QueryDict.__getitem__",
                    functools.partial(if_iast_taint_returned_object_for, OriginType.PARAMETER),
                )
            )
        except Exception:
            log.debug("Unexpected exception while patch IAST functions", exc_info=True)


def _custom_protobuf_getattribute(self, name):
    from ddtrace.appsec._iast._taint_tracking import OriginType
    from ddtrace.appsec._iast._taint_tracking import taint_pyobject
    from ddtrace.appsec._iast._taint_utils import taint_structure

    ret = type(self).__saved_getattr(self, name)
    if isinstance(ret, (str, bytes, bytearray)):
        ret = taint_pyobject(
            pyobject=ret,
            source_name=OriginType.GRPC_BODY,
            source_value=ret,
            source_origin=OriginType.GRPC_BODY,
        )
    elif MessageMapContainer is not None and isinstance(ret, MutableMapping):
        if isinstance(ret, MessageMapContainer) and len(ret):
            # Patch the message-values class
            first_key = next(iter(ret))
            value_type = type(ret[first_key])
            _patch_protobuf_class(value_type)
        else:
            ret = taint_structure(ret, OriginType.GRPC_BODY, OriginType.GRPC_BODY)

    return ret


_custom_protobuf_getattribute.__datadog_custom = True  # type: ignore[attr-defined]


# Used to replace the Protobuf message class "getattribute" with a custom one that taints the return
# of the original __getattribute__ method
def _patch_protobuf_class(cls):
    getattr_method = getattr(cls, "__getattribute__")
    if not getattr_method:
        return

    if not hasattr(getattr_method, "__datadog_custom"):
        try:
            # Replace the class __getattribute__ method with our custom one
            # (replacement is done at the class level because it would incur on a recursive loop with the instance)
            cls.__saved_getattr = getattr_method
            cls.__getattribute__ = _custom_protobuf_getattribute
        except TypeError:
            # Avoid failing on Python 3.12 while patching immutable types
            pass


def _on_grpc_response(message):
    if not _is_iast_enabled():
        return

    msg_cls = type(message)
    _patch_protobuf_class(msg_cls)


def _on_grpc_server_response(message):
    if not _is_iast_enabled():
        return

    asm_context.set_waf_address(SPAN_DATA_NAMES.GRPC_SERVER_RESPONSE_MESSAGE, message)
    _on_grpc_response(message)


def _on_grpc_server_data(headers, request_message, method, metadata):
    if not _is_iast_enabled():
        return

    asm_context.set_headers(headers)
    if request_message is not None:
        asm_context.set_waf_address(SPAN_DATA_NAMES.GRPC_SERVER_REQUEST_MESSAGE, request_message)

    asm_context.set_waf_address(SPAN_DATA_NAMES.GRPC_SERVER_METHOD, method)

    if metadata:
        asm_context.set_waf_address(SPAN_DATA_NAMES.GRPC_SERVER_REQUEST_METADATA, dict(metadata))


def _wsgi_make_block_content(ctx, construct_url):
    middleware = ctx.get_item("middleware")
    req_span = ctx.get_item("req_span")
    headers = ctx.get_item("headers")
    environ = ctx.get_item("environ")
    if req_span is None:
        raise ValueError("request span not found")
    block_config = asm_context.get_blocked()
    desired_type = block_config.get("type", "auto")
    ctype = None
    if desired_type == "none":
        content = ""
        resp_headers = [("content-type", "text/plain; charset=utf-8"), ("location", block_config.get("location", ""))]
    else:
        ctype = block_config.get("content-type", "application/json")
        content = http_utils._get_blocked_template(ctype).encode("UTF-8")
        resp_headers = [("content-type", ctype)]
    status = block_config.get("status_code", 403)
    try:
        req_span.set_tag_str(RESPONSE_HEADERS + ".content-length", str(len(content)))
        if ctype is not None:
            req_span.set_tag_str(RESPONSE_HEADERS + ".content-type", ctype)
        req_span.set_tag_str(http.STATUS_CODE, str(status))
        url = construct_url(environ)
        query_string = environ.get("QUERY_STRING")
        _set_url_tag(middleware._config, req_span, url, query_string)
        if query_string and middleware._config.trace_query_string:
            req_span.set_tag_str(http.QUERY_STRING, query_string)
        method = environ.get("REQUEST_METHOD")
        if method:
            req_span.set_tag_str(http.METHOD, method)
        user_agent = _get_request_header_user_agent(headers, headers_are_case_sensitive=True)
        if user_agent:
            req_span.set_tag_str(http.USER_AGENT, user_agent)
    except Exception as e:
        log.warning("Could not set some span tags on blocked request: %s", str(e))  # noqa: G200
    resp_headers.append(("Content-Length", str(len(content))))
    return status, resp_headers, content


def _asgi_make_block_content(ctx, url):
    middleware = ctx.get_item("middleware")
    req_span = ctx.get_item("req_span")
    headers = ctx.get_item("headers")
    environ = ctx.get_item("environ")
    if req_span is None:
        raise ValueError("request span not found")
    block_config = asm_context.get_blocked()
    desired_type = block_config.get("type", "auto")
    ctype = None
    if desired_type == "none":
        content = ""
        resp_headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"location", block_config.get("location", "").encode()),
        ]
    else:
        ctype = block_config.get("content-type", "application/json")
        content = http_utils._get_blocked_template(ctype).encode("UTF-8")
        # ctype = f"{ctype}; charset=utf-8" can be considered at some point
        resp_headers = [(b"content-type", ctype.encode())]
    status = block_config.get("status_code", 403)
    try:
        req_span.set_tag_str(RESPONSE_HEADERS + ".content-length", str(len(content)))
        if ctype is not None:
            req_span.set_tag_str(RESPONSE_HEADERS + ".content-type", ctype)
        req_span.set_tag_str(http.STATUS_CODE, str(status))
        query_string = environ.get("QUERY_STRING")
        _set_url_tag(middleware.integration_config, req_span, url, query_string)
        if query_string and middleware._config.trace_query_string:
            req_span.set_tag_str(http.QUERY_STRING, query_string)
        method = environ.get("REQUEST_METHOD")
        if method:
            req_span.set_tag_str(http.METHOD, method)
        user_agent = _get_request_header_user_agent(headers, headers_are_case_sensitive=True)
        if user_agent:
            req_span.set_tag_str(http.USER_AGENT, user_agent)
    except Exception as e:
        log.warning("Could not set some span tags on blocked request: %s", str(e))  # noqa: G200
    resp_headers.append((b"Content-Length", str(len(content)).encode()))
    return status, resp_headers, content


def _on_flask_blocked_request(span):
    core.set_item(HTTP_REQUEST_BLOCKED, True)
    span.set_tag_str(http.STATUS_CODE, "403")
    request = core.get_item("flask_request")
    try:
        base_url = getattr(request, "base_url", None)
        query_string = getattr(request, "query_string", None)
        if base_url and query_string:
            _set_url_tag(core.get_item("flask_config"), span, base_url, query_string)
        if query_string and core.get_item("flask_config").trace_query_string:
            span.set_tag_str(http.QUERY_STRING, query_string)
        if request.method is not None:
            span.set_tag_str(http.METHOD, request.method)
        user_agent = _get_request_header_user_agent(request.headers)
        if user_agent:
            span.set_tag_str(http.USER_AGENT, user_agent)
    except Exception as e:
        log.warning("Could not set some span tags on blocked request: %s", str(e))  # noqa: G200


def _on_start_response_blocked(ctx, flask_config, response_headers, status):
    trace_utils.set_http_meta(ctx["req_span"], flask_config, status_code=status, response_headers=response_headers)


def listen():
    core.on("set_http_meta_for_asm", _on_set_http_meta)
    core.on("flask.request_call_modifier", _on_request_span_modifier, "request_body")
    core.on("flask.request_init", _on_request_init)
    core.on("flask.blocked_request_callable", _on_flask_blocked_request)

    core.on("flask.start_response.blocked", _on_start_response_blocked)

    core.on("django.func.wrapped", _on_django_func_wrapped)
    core.on("django.wsgi_environ", _on_wsgi_environ, "wrapped_result")
    core.on("django.patch", _on_django_patch)
    core.on("flask.patch", _on_flask_patch)

    core.on("asgi.request.parse.body", _on_asgi_request_parse_body, "await_receive_and_body")

    core.on("grpc.client.response.message", _on_grpc_response)
    core.on("grpc.server.response.message", _on_grpc_server_response)
    core.on("grpc.server.data", _on_grpc_server_data)

    core.on("wsgi.block.started", _wsgi_make_block_content, "status_headers_content")
    core.on("asgi.block.started", _asgi_make_block_content, "status_headers_content")
