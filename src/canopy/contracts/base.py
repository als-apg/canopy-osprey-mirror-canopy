from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CanopyModel(BaseModel):
    """Pydantic v2 base for all Canopy DTOs.

    Serializes snake_case Python attributes as camelCase on the wire and
    accepts either form on input. No per-field `Field(alias=...)` annotations
    needed — the generator handles every attribute.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        validate_by_name=True,
    )
