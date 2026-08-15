#!/bin/sh
# Deploy to /etc/letsencrypt/renewal-hooks/pre/ -- see README.md in this
# directory for why this exists (certbot's standalone authenticator needs
# port 80 free, HAProxy permanently holds it).
systemctl stop haproxy
