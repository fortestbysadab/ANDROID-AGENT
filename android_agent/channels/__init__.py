"""Channel connectors: message sources outside the device."""

from .base import Channel, ChannelError, Message, as_untrusted_block, truncate
from .gmail import GmailChannel

__all__ = [
    "Channel",
    "ChannelError",
    "GmailChannel",
    "Message",
    "as_untrusted_block",
    "truncate",
]
