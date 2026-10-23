#!/usr/bin/env bash
set -euo pipefail
: "${UBUNTU_A_IP:?Set UBUNTU_A_IP}" "${UBUNTU_B_IP:?Set UBUNTU_B_IP}" "${MAC_IP:?Set MAC_IP}"
echo "Ubuntu A: checking search and Gorse master"
curl -fsS --max-time 3 "http://${UBUNTU_A_IP}:9200" >/dev/null && echo 'OpenSearch OK' || echo 'OpenSearch not ready'
curl -fsS --max-time 3 "http://${UBUNTU_A_IP}:8088" >/dev/null && echo 'Gorse master HTTP OK' || echo 'Gorse master not ready'
echo "Ubuntu B: checking recommendation API"
curl -fsS --max-time 3 "http://${UBUNTU_B_IP}:8087/api/health/ready" || echo 'Gorse server not ready'
echo
echo "Mac: checking commerce API and web"
curl -fsS --max-time 3 http://127.0.0.1:8000/health/ready || echo 'Commerce API not ready'
curl -fsS --max-time 3 "http://${MAC_IP}:3000" >/dev/null && echo 'Storefront OK' || echo 'Storefront not ready'
