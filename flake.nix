{
  description = "codedash — status and upstream distance for every git checkout under a directory";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      nixosModules = {
        default = ./module.nix;
        codedash = ./module.nix;
      };

      packages = forAllSystems (
        pkgs:
        let
          codedash = pkgs.writeShellScriptBin "codedash" ''
            exec ${pkgs.python3}/bin/python3 ${./codedash.py} "$@"
          '';
        in
        {
          default = codedash;
          inherit codedash;
        }
      );

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/codedash";
        };
      });

      checks = forAllSystems (pkgs: {
        # The script is stdlib-only, so "it parses" is the meaningful build
        # check; everything else is behaviour and is exercised by hand.
        syntax = pkgs.runCommand "codedash-syntax" { nativeBuildInputs = [ pkgs.python3 ]; } ''
          python3 -c "compile(open('${./codedash.py}').read(), 'codedash.py', 'exec')"
          touch $out
        '';
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
