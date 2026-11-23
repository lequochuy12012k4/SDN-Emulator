# SDN Emulator

## Desktop application

Install the Desktop dependencies with `pip install -e ".[desktop]"`, then run
`python desktop/main.py`. The Desktop app starts its local `os_ken_controller`
automatically for the default Controller at `127.0.0.1:6653`. Legacy topology
files that used the internal Controller are loaded as OpenFlow topologies.