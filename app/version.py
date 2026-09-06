"""Runtime version metadata shared by the launcher and frozen build."""

__version__ = "0.2.0"
PRODUCT_NAME = "ARGUS"
BUILD_METADATA = {
    "name": "argus-applications",
    "product": PRODUCT_NAME,
    "version": __version__,
}

__all__ = ["BUILD_METADATA", "PRODUCT_NAME", "__version__"]
