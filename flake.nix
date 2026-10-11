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
              ${lib.optionalString pkgs.stdenv.isLinux ''
                # Binary Python wheels with compiled C++ extensions
                # (curl_cffi via httpx-curl-cffi, used for TLS
                # fingerprinting) link against libstdc++.so.6 at
                # runtime. The Nix devShell doesn't expose the host's
                # library paths; surface it from the Nix store.
                export LD_LIBRARY_PATH="${lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
              ''}
              echo "🐍 python: $(python3 --version)"
              echo "📦 uv:     $(uv --version)"
            '';
          };
        });

      packages = forAllSystems (system:
        let
          pkgs = pkgsFor system;
          python = pkgs.${pyAttr};
          # A remote test host puts only this env's bin on PATH and clears
          # every UV_* variable first, so the devShell's interpreter pin and
          # library path have to travel inside the uv binary itself. The pin
          # applies on Darwin too, so a uv-managed Python on the host can't
          # outrank the Nix one.
          uv = pkgs.symlinkJoin {
            name = "uv-nix-python";
            paths = [ pkgs.uv ];
            nativeBuildInputs = [ pkgs.makeWrapper ];
            postBuild = ''
              wrapProgram $out/bin/uv \
                --set UV_PYTHON ${python}/bin/python3 \
                --set UV_PYTHON_PREFERENCE only-system \
                ${lib.optionalString pkgs.stdenv.isLinux
                  "--prefix LD_LIBRARY_PATH : ${lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]}"}
            '';
          };
        in
        {
          # What a remote test run needs to sync the locked env and run pytest.
          factory-tools = pkgs.buildEnv {
            name = "factory-tools";
            paths = [ python uv pkgs.git ];
          };
        });

      # To `nix build` the project itself (for nixpkgs PRs, NixOS modules,
      # home-manager), enable uv2nix: https://pyproject-nix.github.io/uv2nix/
      # Then also add `nix build .#default` to ci.yml.
      #
      # packages.default = ...;
      # apps = forAllSystems (system: { default = ...; });
    };
}
