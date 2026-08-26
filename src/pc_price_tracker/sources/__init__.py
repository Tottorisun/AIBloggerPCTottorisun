from pc_price_tracker.sources.affiliate_feed import AdmitadSource, AffiliateFeedSource, EpnSource, FeedNotConfigured
from pc_price_tracker.sources.base import BaseSource, SourceBlocked
from pc_price_tracker.sources.citilink import CitilinkSource
from pc_price_tracker.sources.regard import RegardSource

SOURCES: dict[str, type[BaseSource]] = {
    RegardSource.name: RegardSource,
    CitilinkSource.name: CitilinkSource,
    AdmitadSource.network_name: AdmitadSource,
    EpnSource.network_name: EpnSource,
}

__all__ = [
    "BaseSource",
    "SourceBlocked",
    "SOURCES",
    "RegardSource",
    "CitilinkSource",
    "AffiliateFeedSource",
    "AdmitadSource",
    "EpnSource",
    "FeedNotConfigured",
]
