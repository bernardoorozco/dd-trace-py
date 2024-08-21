#    Copyright (C) 2015 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import wrapt

from . import _utils

_KWARG_RENAMED_POSTFIX_TPL = ", please use the '%s' argument instead"
_KWARG_RENAMED_PREFIX_TPL = "Using the '%s' argument is deprecated"


def renamed_kwarg(old_name, new_name, message=None,
                  version=None, removal_version=None, stacklevel=3,
                  category=None, replace=False):
    """Decorates a kwarg accepting function to deprecate a renamed kwarg."""

    prefix = _KWARG_RENAMED_PREFIX_TPL % old_name
    postfix = _KWARG_RENAMED_POSTFIX_TPL % new_name
    out_message = _utils.generate_message(
        prefix, postfix=postfix, message=message, version=version,
        removal_version=removal_version)

    @wrapt.decorator
    def decorator(wrapped, instance, args, kwargs):
        if old_name in kwargs:
            _utils.deprecation(out_message,
                               stacklevel=stacklevel, category=category)
            if replace:
                kwargs.setdefault(new_name, kwargs.pop(old_name))
        return wrapped(*args, **kwargs)

    return decorator
