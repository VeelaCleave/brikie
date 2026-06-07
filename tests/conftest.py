"""Shared pytest fixtures for the Brikie test suite."""

import asyncio

import pytest

from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry
from brikie.kernel.state import StateManager


@pytest.fixture
def event_loop():
    """Create a fresh asyncio event loop for each test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def state_manager():
    """Provide a fresh StateManager instance for each test."""
    return StateManager()


@pytest.fixture
def hook_dispatcher():
    """Provide a fresh HookDispatcher instance for each test."""
    return HookDispatcher()


@pytest.fixture
def brick_registry():
    """Provide a fresh BrickRegistry instance for each test."""
    return BrickRegistry()
