"""A live page for a Bazaar client, fed by a tap inside the client's process.

``hub.Dashboard`` remembers one session, ``view.render`` turns it into the
page's JSON, ``server.DashboardServer`` serves the page, and
``listener.TapListener`` receives what the tap sends.  ``install`` connects a
client; see the README for the whole picture.
"""
