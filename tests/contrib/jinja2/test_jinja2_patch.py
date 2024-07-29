# This test script was automatically generated by the contrib-patch-tests.py
# script. If you want to make changes to it, you should make sure that you have
# removed the ``_generated`` suffix from the file name, to prevent the content
# from being overwritten by future re-generations.

from ddtrace.contrib.jinja2.patch import _get_version
from ddtrace.contrib.jinja2.patch import patch


try:
    from ddtrace.contrib.jinja2.patch import unpatch
except ImportError:
    unpatch = None
from tests.contrib.patch import PatchTestCase


class TestJinja2Patch(PatchTestCase.Base):
    __integration_name__ = "jinja2"
    __module_name__ = "jinja2"
    __patch_func__ = patch
    __unpatch_func__ = unpatch
    __get_version__ = _get_version

    def assert_module_patched(self, jinja2):
        pass

    def assert_not_module_patched(self, jinja2):
        pass

    def assert_not_module_double_patched(self, jinja2):
        pass
