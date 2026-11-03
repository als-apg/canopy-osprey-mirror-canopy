"""The in-process gateway surface that other Canopy modules consume."""

from pathlib import Path

import pytest

from canopy.converter import gateway
from canopy.converter.services.conversion_service import ConversionService

FIXTURES = Path(__file__).parent / "fixtures"


async def test_convert_screen_returns_validated_screen_dict():
    gateway.bind(ConversionService())
    try:
        screen = await gateway.convert_screen((FIXTURES / "widgets.edl").read_bytes(), kind="edl")
    finally:
        gateway.bind(None)
    assert screen["schemaVersion"] == "1.0"
    assert screen["id"] == "screen"
    assert screen["root"]["type"] == "absolute-canvas"


async def test_convert_screen_filename_names_the_screen():
    gateway.bind(ConversionService())
    try:
        screen = await gateway.convert_screen(
            (FIXTURES / "widgets.edl").read_bytes(), kind="edl", filename="widgets.edl"
        )
    finally:
        gateway.bind(None)
    assert screen["id"] == "widgets"


async def test_convert_screen_before_bind_raises():
    gateway.bind(None)
    with pytest.raises(RuntimeError, match="before app startup"):
        await gateway.convert_screen(b"x", kind="edl")


async def test_convert_screen_accepts_bob():
    bob = b"""<?xml version="1.0" encoding="UTF-8"?>
<display version="2.0.0">
  <name>Synthetic</name>
  <widget type="label" version="2.0.0">
    <name>Title</name>
    <text>Hello</text>
  </widget>
</display>
"""
    gateway.bind(ConversionService())
    try:
        screen = await gateway.convert_screen(bob, kind="bob")
    finally:
        gateway.bind(None)
    assert screen["schemaVersion"] == "1.0"
    assert screen["root"]["children"]
