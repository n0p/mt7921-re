#!/bin/sh
# Run a command on the lab host lorien (X410 SDR, builds). Quiet: no host-key art.
exec ssh -o BatchMode=yes -o ConnectTimeout=15 -o VisualHostKey=no 155.54.95.95 "$@"
