#!/bin/bash
source /home/freqtrade/.freqtrade.env
curl -s -u "$FREQUI_USERNAME:$FREQUI_PASSWORD" http://127.0.0.1:8090/system
