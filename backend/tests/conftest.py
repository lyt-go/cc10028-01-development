"""测试夹具：每个 pytest 会话使用独立的临时数据目录，避免运行产物互相污染。

后端把解析产物、分块、检查点等写到 ``DATA_ROOT``；测试如果共用仓库下的
``data/``，重试次数等检查点会跨会话累计，导致同一测试第二次运行失败。
"""
import os
import shutil
import tempfile

_SESSION_DATA_DIR = tempfile.mkdtemp(prefix="docparse-test-data-")
os.environ["APP_DATA_DIR"] = _SESSION_DATA_DIR


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001 - pytest 钩子签名
    shutil.rmtree(_SESSION_DATA_DIR, ignore_errors=True)
