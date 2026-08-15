#!/bin/sh
# Deploy to /etc/letsencrypt/renewal-hooks/deploy/ -- see README.md in this
# directory for why this exists (HAProxy reads a separate manually-bundled
# cert+key file that certbot's own live/ directory never updates directly).
cat /etc/letsencrypt/live/erp16.orion-instruments.io/fullchain.pem /etc/letsencrypt/live/erp16.orion-instruments.io/privkey.pem > /etc/ssl/private/erp16.orion-instruments.io.pem
chmod 600 /etc/ssl/private/erp16.orion-instruments.io.pem
