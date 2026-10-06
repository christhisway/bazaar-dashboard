"""The game's wire format, decoded into plain frozen dataclasses.

``decode`` reads frames in both directions, ``model`` is what they become, and
``store`` holds the few questions the page asks of a state.  Bindings are
generated into ``generated/`` from ``proto/bazaar.proto`` by ``setup``.
"""
