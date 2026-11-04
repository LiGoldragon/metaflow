{
  description = "Minimal foreground composition of the version-one metaflow Nexuses";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };
  outputs = { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        deployment = import ./deployment.nix;
        makeFlowPair = import ./nix/flow-pair.nix { inherit pkgs; };
        flowPair = makeFlowPair deployment.flow;
        runtimeDeployment = deployment // {
          flow = deployment.flow // { pair = "${flowPair}/bin/flow-pair"; };
        };
        deploymentJson = pkgs.writeText "metaflow-deployment.json" (builtins.toJSON runtimeDeployment);
        metaflow = pkgs.writeShellScriptBin "metaflow" ''
          export METAFLOW_DEPLOYMENT_CONFIG=${deploymentJson}
          exec ${pkgs.python3}/bin/python3 ${./src/metaflow.py} "$@"
        '';

        # A separate test deployment: its one trusted path is an exclusively
        # built store directory. The product deployment above stays empty.
        checkWorkspace = pkgs.runCommand "metaflow-check-workspace" {} ''mkdir -p "$out"'';
        trustedTestFlowPair = makeFlowPair (deployment.flow // {
          trustedCodexPaths = [ "${checkWorkspace}" ];
        });
        trustedTestDeployment = deployment // {
          flow = deployment.flow // {
            pair = "${trustedTestFlowPair}/bin/flow-pair";
            trustedCodexPaths = [ "${checkWorkspace}" ];
          };
        };
        trustedTestJson = pkgs.writeText "metaflow-trusted-test-deployment.json"
          (builtins.toJSON trustedTestDeployment);
        trustedTestMetaflow = pkgs.writeShellScriptBin "metaflow" ''
          export METAFLOW_DEPLOYMENT_CONFIG=${trustedTestJson}
          exec ${pkgs.python3}/bin/python3 ${./src/metaflow.py} "$@"
        '';

        # A fake engine proves exact argv projection without starting a Nexus.
        # store directory. The product deployment above stays empty.
        captureEngine = pkgs.writeShellScript "capture-flow-engine" ''
          printf '%s\n' "$@" > "$FLOW_PAIR_CAPTURE"
        '';
        inert = pkgs.writeShellScript "inert-component" ''exit 0'';
        checkFlow = deployment.flow // {
          pairExecutable = "${captureEngine}";
          daemonExecutable = "${inert}";
          translatorExecutable = "${inert}";
          trustedCodexPaths = [ "${checkWorkspace}" ];
        };
        checkFlowPair = makeFlowPair checkFlow;
        expectedArguments = pkgs.writeText "flow-pair-expected-arguments"
          (builtins.concatStringsSep "\n" [
            "{ check «/runtime» «/state» }"
            "${inert}"
            "${inert}"
            "70000"
            "8388608"
            "25"
            "1048576"
            "64"
            "${checkWorkspace}"
            ""
          ]);
        invalidRelativeTrust = builtins.tryEval (makeFlowPair (deployment.flow // {
          trustedCodexPaths = [ "relative/workspace" ];
        }));
        invalidBroadTrust = builtins.tryEval (makeFlowPair (deployment.flow // {
          trustedCodexPaths = [ "/" ];
        }));
        invalidDuplicateTrust = builtins.tryEval (makeFlowPair (deployment.flow // {
          trustedCodexPaths = [ "/exact/workspace" "/exact/workspace" ];
        }));
      in {
        packages = {
          default = metaflow;
          flow-pair = flowPair;
          trusted-test = trustedTestMetaflow;
        };
        apps.default = { type = "app"; program = "${metaflow}/bin/metaflow"; };
        checks = {
          unit = pkgs.runCommand "metaflow-unit" { nativeBuildInputs = [ pkgs.python3 ]; } ''
            cp -r ${./src} src
            cp -r ${./tests} tests
            chmod -R u+w .
            PYTHONPATH=$PWD/src python3 -m unittest discover -v tests
            touch $out
          '';
          trusted-flow-pair = pkgs.runCommand "metaflow-trusted-flow-pair" {} ''
            export FLOW_PAIR_CAPTURE=$PWD/arguments
            ${checkFlowPair}/bin/flow-pair '{ check «/runtime» «/state» }'
            diff -u ${expectedArguments} "$FLOW_PAIR_CAPTURE"
            # The public surface remains exactly one Place.
            if ${checkFlowPair}/bin/flow-pair one two >/dev/null 2>&1; then exit 1; fi
            touch $out
          '';
          default-trust-empty = pkgs.runCommand "metaflow-default-trust-empty" {} ''
            test ${toString (builtins.length deployment.flow.trustedCodexPaths)} -eq 0
            test ${if invalidRelativeTrust.success then "1" else "0"} -eq 0
            test ${if invalidBroadTrust.success then "1" else "0"} -eq 0
            test ${if invalidDuplicateTrust.success then "1" else "0"} -eq 0
            if grep -F ${checkWorkspace} ${deploymentJson}; then exit 1; fi
            grep -F -q ${checkWorkspace} ${trustedTestJson}
            grep -F -q ${checkWorkspace} ${trustedTestFlowPair}/bin/flow-pair
            touch $out
          '';
        };
        devShells.default = pkgs.mkShell { packages = [ pkgs.python3 ]; };
      });
}
