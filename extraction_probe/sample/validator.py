"""Order validation rules."""

from __future__ import annotations

# Shipping-method-specific minimum and maximum weights (kg)
SHIPPING_LIMITS: dict[str, tuple[float, float]] = {
    "standard": (0.01, 50.0),
    "express": (0.01, 20.0),
    "freight": (10.0, 5_000.0),
}

DEFAULT_LIMITS = (0.01, 25.0)


def validate_order_weight(weight_kg: float, shipping_method: str) -> None:
    """Validate order weight against shipping-method-specific limits.

    Raises ValueError if the weight is outside the allowed range.
    """
    min_weight, max_weight = SHIPPING_LIMITS.get(shipping_method, DEFAULT_LIMITS)

    if weight_kg < min_weight:
        raise ValueError(
            f"Weight {weight_kg} kg below minimum {min_weight} for {shipping_method}"
        )
    if weight_kg > max_weight:
        raise ValueError(
            f"Weight {weight_kg} kg exceeds maximum {max_weight} for {shipping_method}"
        )
