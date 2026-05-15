{
  description = "flight-cli: ITA Matrix Alkali backend wrapper (uv-managed; Nix dev shell optional).";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, pyproject-nix }:
    let
      inherit (nixpkgs) lib;
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAllSystems = lib.genAttrs systems;
      pkgsFor = system: nixpkgs.legacyPackages.${system};

      # Python interpreter follows pyproject.toml's `requires-python`: the
      # first PEP 440 spec is taken as the floor (handles `>=X.Y` and
      # `>=X.Y,<Z.W` shapes). Bump pyproject.toml — shell follows.
      project = pyproject-nix.lib.project.loadPyproject { projectRoot = ./.; };
      lowerBound = lib.head project.requires-python;
      pyAttr =
        "python"
        + toString (builtins.elemAt lowerBound.version.release 0)
        + toString (builtins.elemAt lowerBound.version.release 1);
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = pkgsFor system;
          python = pkgs.${pyAttr};
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.uv
              pkgs.gnumake
            ];

            # uv's bundled python-build-standalone won't link on NixOS;
            # force the Nix-managed interpreter. No-op on Darwin.
            shellHook = ''
              export UV_PYTHON=${python}/bin/python3
              export UV_PYTHON_PREFERENCE=only-system
              echo "🐍 python: $(python3 --version)"
              echo "📦 uv:     $(uv --version)"
            '';
          };
        });

      # To `nix build` this project (for nixpkgs PRs, NixOS modules,
      # home-manager), enable uv2nix: https://pyproject-nix.github.io/uv2nix/
      # Then also add `nix build .#default` to ci.yml.
      #
      # packages = forAllSystems (system: { default = ...; });
      # apps = forAllSystems (system: { default = ...; });
    };
}
