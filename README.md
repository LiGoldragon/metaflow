# metaflow version one shell

`metaflow start <name>` foregrounds the exact pinned Herdr, Belief and Flow
pairs. It derives one typed `Place` for all three from the XDG runtime and state
roots. It waits for typed readiness, admits only the verified Flow translator
child to Herdr and Belief, and then remains quiet until a child ends or it is
stopped.

Repository registration and Flow definitions are owner CLI input. The launcher
never writes them. It never restarts a pair. State survives every exit.

The deployment values and exact immutable package paths are in
`deployment.nix`. The owner registers belief repositories; Flow only furnishes
them.

```sh
nix run . -- start main
```
