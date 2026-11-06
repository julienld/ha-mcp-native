"""Pure camera content labels shared by native and compatibility paths."""

from datetime import datetime

_JPEG_ALIASES = {"jpg": "jpeg", "jpe": "jpeg", "jif": "jpeg"}


def _detect_image_format(content_type: str) -> str:
    """Report the declared image format from a Content-Type header.

    Only the media type's subtype is relevant (``image/jpeg`` -> ``jpeg``);
    parameters such as ``; q=0.9`` are stripped and JPEG's alternate
    spellings are folded in. Unknown image subtypes pass through verbatim —
    relabelling ``image/webp`` as JPEG would misidentify the Image block.
    The JPEG default applies only when the header is missing, empty, or
    not an ``image/*`` type with a subtype (e.g. ``application/octet-stream``).
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    primary, _, subtype = media_type.partition("/")
    if primary != "image" or not subtype:
        return "jpeg"
    return _JPEG_ALIASES.get(subtype, subtype)


def _snapshot_info_text(
    image_format: str,
    image_size: tuple[int, int] | None,
    retrieved: datetime,
    timezone_fallback: bool = False,
) -> str:
    """Build the text half of the camera image response.

    Reports the size of the image actually served (which may differ from
    the camera's native resolution when Home Assistant rescaled it) and
    when the snapshot was retrieved, in Home Assistant local time with
    UTC offset. When the timezone lookup fell back to UTC, the text says
    so, so the label cannot be mistaken for a genuine UTC install.

    The block deliberately describes only the bytes that were served —
    never the entity's name or state. Home Assistant can mark an entity
    ``llm_exposure: hidden``, asking the LLM to know nothing about that
    entity; that setting does not un-expose this tool, so entity data in
    the text would keep reporting on the camera despite the setting.
    """
    prefix = f"Camera snapshot ({image_format.upper()}"
    if image_size is None:
        detail = f"{prefix})"
    else:
        width, height = image_size
        detail = f"{prefix}, {width}x{height})"
    note = (
        " (UTC — could not determine the Home Assistant timezone)"
        if timezone_fallback
        else ""
    )
    return f"{detail}. Retrieved: {retrieved:%Y-%m-%d %H:%M:%S %:z}{note}"
