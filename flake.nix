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
        deploymentJson = pkgs.writeText "metaflow-deployment.json" (builtins.toJSON deployment);
        metaflow = pkgs.writeShellScriptBin "metaflow" ''
          export METAFLOW_DEPLOYMENT_CONFIG=${deploymentJson}
          exec ${pkgs.python3}/bin/python3 ${./src/metaflow.py} "$@"
        '';
      in {
        packages.default = metaflow;
        apps.default = { type = "app"; program = "${metaflow}/bin/metaflow"; };
        checks.unit = pkgs.runCommand "metaflow-unit" { nativeBuildInputs = [ pkgs.python3 ]; } ''
          cp -r ${./src} src
          cp -r ${./tests} tests
          chmod -R u+w .
          PYTHONPATH=$PWD/src python3 -m unittest discover -v tests
          touch $out
        '';
        devShells.default = pkgs.mkShell { packages = [ pkgs.python3 ]; };
      });
}
