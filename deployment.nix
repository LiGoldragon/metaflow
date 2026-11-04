{
  runtimeRootEnvironment = "XDG_RUNTIME_DIR";
  stateRootEnvironment = "XDG_STATE_HOME";
  stateRootFallback = ".local/state";
  runtimeNamespace = "metaflow";
  stateNamespace = "metaflow";
  startupTimeoutSeconds = 20;
  commandTimeoutSeconds = 10;
  bootstrapPollMilliseconds = 50;
  stopGraceSeconds = 3;
  uidRule = "current-login";

  flow = {
    package = "/nix/store/fx97zi3zql2rm1s5xpllx1dbdfd3axdc-flow-nexus-v1";
    pair = "/nix/store/fx97zi3zql2rm1s5xpllx1dbdfd3axdc-flow-nexus-v1/bin/flow-pair";
    pairExecutable = "/nix/store/ykc19hndw3rjhcrvm06xqqzlxc2lwjq5-flow-nexus-engines-1.0.0/libexec/flow-pair-engine";
    daemonExecutable = "/nix/store/i3cijwhxx6jb86iw2zsk1x60764b5y1h-flow-nexus-daemon-1.0.0/bin/flow-nexus-daemon";
    translatorExecutable = "/nix/store/srqgh07cz5mffkwzk86k4g14qdvsj9ap-flow-nexus-translator-1.0.0/bin/flow-nexus-translator";
    cli = "/nix/store/fx97zi3zql2rm1s5xpllx1dbdfd3axdc-flow-nexus-v1/bin/flow-nexus";
    sockets = [ "flow.sock" "flow-translator.sock" ];
    readyDatom = "Definitions";
  };

  belief = {
    package = "/nix/store/3v2wkqf3wj3miw1kq66v72fv5136wlwb-belief-nexus-v1";
    pair = "/nix/store/3v2wkqf3wj3miw1kq66v72fv5136wlwb-belief-nexus-v1/bin/belief-pair";
    pairExecutable = "/nix/store/qs3pds1j86515rlaipn7zcn8bmp5q8xg-belief-nexus-internal-0.1.0/libexec/belief-pair";
    daemonExecutable = "/nix/store/w1zkxhbqw4zvnmrlcxjc1zix7prhlyl9-belief-nexus-daemon-0.1.0/bin/belief-nexus-daemon";
    translatorExecutable = "/nix/store/rij54ybza6rbqdgj1v33vijgwsy4jjzj-belief-translator-0.1.0/bin/belief-translator";
    cli = "/nix/store/3v2wkqf3wj3miw1kq66v72fv5136wlwb-belief-nexus-v1/bin/belief-nexus";
    metaCli = "/nix/store/3v2wkqf3wj3miw1kq66v72fv5136wlwb-belief-nexus-v1/bin/belief-nexus-meta";
    sockets = [ "belief.sock" "belief-meta.sock" "belief-translator.sock" "belief-meta-translator.sock" ];
    resolveDatom = "Resolve.[]";
    readyDatom = "Configure.{ None RepositoryPolling.211 }";
    peerDatom = "Configure.{ None OrdinaryPeers.[ { flow { %UID% %PID% } } ] }";
  };

  herdr = {
    package = "/nix/store/51k5a966vki9d8ipah1p5kjj18mmbawv-herdr-nexus-v1";
    pair = "/nix/store/51k5a966vki9d8ipah1p5kjj18mmbawv-herdr-nexus-v1/bin/herdr-pair";
    pairExecutable = "/nix/store/rsjxkn27mggh7xdahb4srwpn75p76x59-herdr-nexus-cli-0.1.0/libexec/herdr-pair-launcher";
    daemonExecutable = "/nix/store/gb1y4kj613x2wc5nw786zrl72clqwrmx-herdr-nexus-daemon-0.1.0/bin/herdr-nexus-daemon";
    translatorExecutable = "/nix/store/pg16fqvdbn9axv660srbl9jlvz1m81sr-herdr-translator-0.1.0/bin/herdr-translator";
    metaCli = "/nix/store/51k5a966vki9d8ipah1p5kjj18mmbawv-herdr-nexus-v1/bin/herdr-nexus-meta";
    herdr = "/nix/store/9x03bz0q0a978zrzcshrlmmb4fvcmdb6-herdr-0.8.2/bin/herdr";
    sockets = [ "herdr-meta.sock" "herdr-translator.sock" "herdr-meta-translator.sock" ];
    lateSockets = [ "herdr.sock" ];
    readyDatoms = [
      "Configure.HerdrPath.%HERDR%"
      "Configure.CallTimeout.70000"
      "Configure.OutputLimit.8388608"
      "Configure.TextBudget.{ 1048576 64 }"
      "Configure.StartTimeout.70000"
      "Configure.Polling.100"
      "Configure.FrameCapacity.8388608"
      "Configure.SubscriberCapacity.64"
      "Configure.TransportTimeout.70000"
    ];
    peerDatom = "Configure.Peer.{ flow { %UID% %PID% } }";
  };
}
