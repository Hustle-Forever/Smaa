"""One-off operator scripts that run inside the backend image.

A regular package (not a namespace one) so `python -m scripts.<name>` from
backend/ always finds these, even where site-packages has its own `scripts`.
"""
