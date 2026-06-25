"""Typed ``Annotated[..., Depends(...)]`` aliases for the converter API layer.

Routes pull these in by alias rather than re-spelling ``Depends()``.
"""

from typing import Annotated

from fastapi import Depends

from canopy.converter import gateway
from canopy.converter.config import ConverterConfig, get_converter_config
from canopy.converter.adapters.fox_validation import FoxValidate
from canopy.converter.services.conversion_service import ConversionService


def get_conversion_service() -> ConversionService:
    return gateway.get_service()


def get_config() -> ConverterConfig:
    return get_converter_config()


def get_fox_validate() -> FoxValidate:
    # Lazy import keeps converter module-load independent of Fox; the Fox gateway
    # is the sanctioned cross-module surface (validate = parse + AST-check, no I/O).
    from canopy.fox import gateway as fox_gateway

    return fox_gateway.validate


ConversionServiceDep = Annotated[ConversionService, Depends(get_conversion_service)]
ConverterConfigDep = Annotated[ConverterConfig, Depends(get_config)]
FoxValidateDep = Annotated[FoxValidate, Depends(get_fox_validate)]
