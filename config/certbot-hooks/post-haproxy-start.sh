#!/bin/sh
# Deploy to /etc/letsencrypt/renewal-hooks/post/ -- see README.md in this
# directory for why this exists (pairs with pre-haproxy-stop.sh).
systemctl start haproxy
