#!/bin/sh
# Run a command in the test guest (QEMU VM on the lab host, sshd forwarded to host:2222).
exec ssh -o BatchMode=yes -o ConnectTimeout=15 -o VisualHostKey=no -o "ProxyCommand=ssh -o BatchMode=yes -o VisualHostKey=no -W %h:%p 155.54.95.95" -p 2222 root@localhost "$@"
