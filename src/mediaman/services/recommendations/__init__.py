"""recommendations package — OpenAI-powered media recommendations.

Public entry point: :func:`refresh_recommendations`.
"""

from mediaman.services.recommendations.persist import refresh_recommendations

__all__ = ["refresh_recommendations"]
