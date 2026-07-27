#!/bin/bash
# Prefix each stdin line with UTC date/time (for scanner log files).
while IFS= read -r line || [ -n "$line" ]; do
  printf '%s %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$line"
done
