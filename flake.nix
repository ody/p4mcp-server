{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
  };
  outputs = { self, nixpkgs }: {
    devShell = with nixpkgs.lib; genAttrs systems.flakeExposed (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config = {
            allowUnfree = true;
          };
        };
      in
      with pkgs; mkShell {
        packages = [
          python3
          uv
        ];
      }
    );
  };
}
