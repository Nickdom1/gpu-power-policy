lib:
builtins.listToAttrs (map (line:
  let m = builtins.match "([a-z_][a-z0-9_]*)=([a-z0-9._/-]+|'[a-z0-9._/ -]+')" line;
  in if m == null then throw "pins.sh requires literal KEY=value assignments" else {
    name = builtins.elemAt m 0;
    value = lib.removePrefix "'" (lib.removeSuffix "'" (builtins.elemAt m 1));
  }) (lib.filter (line: line != "" && !lib.hasPrefix "#" line)
    (lib.splitString "\n" (builtins.readFile ../install/pins.sh))))
