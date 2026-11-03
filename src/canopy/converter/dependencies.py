"""Typed ``Annotated[..., Depends(...)]`` aliases for the converter API layer.

Routes pull these in by alias rather than re-spelling ``Depends()``.
"""

from typing import Annotated

from fastapi import Depends

from canopy.converter import gateway
from canopy.converter.config import ConverterConfig, get_converter_config
from canopy.converter.services.conversion_service import ConversionService
from canopy.converter.adapters.formula_engine_validation import FormulaEngineValidate


def get_conversion_service() -> ConversionService:
    return gateway.get_service()


def get_config() -> ConverterConfig:
    return get_converter_config()


def get_formula_engine_validate() -> FormulaEngineValidate:
    # Lazy import keeps converter module-load independent of the Formula Engine; its gateway
    # is the sanctioned cross-module surface (validate = parse + AST-check, no I/O).
    from canopy.formula_engine import gateway as formula_engine_gateway

    return formula_engine_gateway.validate


ConversionServiceDep = Annotated[ConversionService, Depends(get_conversion_service)]
ConverterConfigDep = Annotated[ConverterConfig, Depends(get_config)]
FormulaEngineValidateDep = Annotated[FormulaEngineValidate, Depends(get_formula_engine_validate)]
