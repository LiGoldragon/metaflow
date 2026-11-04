{ pkgs }:
flow:
let
  trusted = flow.trustedCodexPaths or [];
  allStrings = builtins.all builtins.isString trusted;
  exactAbsolute = builtins.all (path:
    pkgs.lib.hasPrefix "/" path
    && path != "/"
    && !(pkgs.lib.hasSuffix "/" path)
    && !(pkgs.lib.hasInfix "//" path)
    && !(pkgs.lib.hasInfix "/./" path)
    && !(pkgs.lib.hasInfix "/../" path)
    && !(pkgs.lib.hasSuffix "/." path)
    && !(pkgs.lib.hasSuffix "/.." path)
  ) trusted;
  unique = builtins.length trusted == builtins.length (pkgs.lib.unique trusted);
  admission = flow.admission;
  textBudget = flow.textBudget;
  trustedArguments = pkgs.lib.escapeShellArgs trusted;
in
assert allStrings;
assert exactAbsolute;
assert unique;
pkgs.writeShellScriptBin "flow-pair" ''
  if [[ $# -ne 1 ]]; then echo "usage: flow-pair '<Place>'" >&2; exit 64; fi
  exec ${flow.pairExecutable} "$1" \
    ${flow.daemonExecutable} \
    ${flow.translatorExecutable} \
    ${toString admission.timeoutMilliseconds} \
    ${toString admission.frameCapacityBytes} \
    ${toString admission.attempts} \
    ${toString textBudget.nodeLimit} \
    ${toString textBudget.depthLimit} \
    ${trustedArguments}
''
