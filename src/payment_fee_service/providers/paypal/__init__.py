from __future__ import annotations

from payment_fee_service.providers.paypal.provider import PayPalProvider as Provider
from payment_fee_service.providers.paypal.repository import PayPalRepository as Repository

__all__ = ["Provider", "Repository"]
