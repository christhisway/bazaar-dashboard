"""Questions the page asks of one state: which offers are open, whose listing.

Plain functions of a state, nothing remembered.  A state lists only offers we
are party to, with their whole history, so "open" has to be read off each
offer's status and expiry rather than assumed from its presence.
"""

from . import model


def is_open(offer: model.Offer) -> bool:
    return offer.status is model.OfferStatus.OFFER_STATUS_OPEN


def is_expired(obj, tick: int) -> bool:
    """``expires_tick`` is the tick at which a thing stops being usable."""
    return tick >= obj.expires_tick


def my_open_offers(state: model.State) -> list[model.Offer]:
    """Our own outgoing offers that are still open."""
    return [
        offer
        for offer in state.offers
        if offer.proposer_id == state.self_station_id and is_open(offer)
    ]


def incoming_open_offers(state: model.State) -> list[model.Offer]:
    """Offers addressed to us and still open, which we could accept."""
    return [
        offer
        for offer in state.offers
        if offer.recipient_id == state.self_station_id
        and offer.proposer_id != state.self_station_id
        and is_open(offer)
        and not is_expired(offer, state.tick)
    ]


def my_active_advertisement(state: model.State) -> model.Advertisement | None:
    """Our current listing, if any.  Each station has at most one active."""
    for advertisement in state.advertisements:
        if (
            advertisement.station_id == state.self_station_id
            and advertisement.status is model.PublicationStatus.PUBLICATION_STATUS_ACTIVE
        ):
            return advertisement
    return None


def peer_active_advertisements(state: model.State) -> list[model.Advertisement]:
    """Everyone else's current listings, sorted by station ID."""
    peers = [
        advertisement
        for advertisement in state.advertisements
        if advertisement.station_id != state.self_station_id
        and advertisement.status is model.PublicationStatus.PUBLICATION_STATUS_ACTIVE
        and not is_expired(advertisement, state.tick)
    ]
    return sorted(peers, key=lambda a: a.station_id)


def remaining_offer_slots(state: model.State) -> int:
    return max(0, state.rules.max_open_outgoing_offers - len(my_open_offers(state)))
