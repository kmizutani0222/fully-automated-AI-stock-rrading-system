from __future__ import annotations

from urllib.parse import urlparse


def database_name(url: str) -> str:
    path = urlparse(url).path.lstrip("/")
    return path.split("?", 1)[0].strip()


def assert_pytest_database(operational_url: str, test_url: str) -> str:
    """Return the test URL, or refuse before pytest can open the operational database."""
    if not test_url.strip():
        raise RuntimeError(
            "pytest refused to start because TEST_DATABASE_URL is not set. "
            "The operational database is not used for tests."
        )
    if not operational_url.strip():
        raise RuntimeError(
            "pytest refused to start because DATABASE_URL is not set, "
            "so the operational database cannot be identified."
        )
    operational = database_name(operational_url)
    test = database_name(test_url)
    if not operational or not test:
        raise RuntimeError("pytest refused to start because a database name is missing from the URL.")
    if operational == test:
        raise RuntimeError(
            "pytest refused to start because TEST_DATABASE_URL points at the operational database "
            f"'{operational}'."
        )
    return test_url


def assert_runtime_database(url: str) -> None:
    """The trading process must not boot on the test database."""
    name = database_name(url)
    if name == "asts_test" or name.endswith("_test"):
        raise RuntimeError(
            "The trading process refuses to start because DATABASE_URL points at the test database "
            f"'{name}'."
        )
