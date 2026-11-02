"""Global pytest configuration and fixtures for Signalpost Agent."""

import pytest
from app.services.job_verifier import JobVerifier


@pytest.fixture(autouse=True)
def reset_nav_circuit_breaker():
    """Ensure clean NAV circuit breaker state before and after each test."""
    JobVerifier.reset_circuit_breaker()
    yield
    JobVerifier.reset_circuit_breaker()
