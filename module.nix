{
  lib,
  pkgs,
  config,
  ...
}:

let
  cfg = config.services.codedash;
  python = pkgs.python3;

  cli = pkgs.writeShellScriptBin "codedash" ''
    exec ${python}/bin/python3 ${./codedash.py} "$@"
  '';

  # The curated list of checkouts to track. The daemon re-reads it on every
  # scan, so `codedash add` takes effect without a restart; it lives in the
  # user's config because only they write it.
  reposFile = "${config.users.users.${cfg.user}.home}/.config/codedash/repos";
in
{
  options.services.codedash = {
    enable = lib.mkEnableOption "codedash, a dashboard for local git checkouts";

    user = lib.mkOption {
      type = lib.types.str;
      description = ''
        The user whose checkouts are scanned. The daemon runs as this user: it
        has to read their home directory, which rules out DynamicUser.
      '';
    };

    root = lib.mkOption {
      type = lib.types.str;
      description = ''
        Directory holding the checkouts. A string on purpose: a Nix path would
        copy the whole tree into the store and the dashboard would then report
        on a snapshot instead of the live repositories.
      '';
      example = "/home/alice/Documents/git";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8090;
      description = "Loopback port for the dashboard. Front it with a reverse proxy or Tailscale.";
    };

    allowActions = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Allow fetch, fast-forward and rebase over HTTP. These are the only
        writes the service performs, they are gated by preconditions (clean
        tree, no operation in progress), and with this off the daemon is a
        read-only dashboard. Anyone who can reach the port can trigger them,
        so keep it on loopback or behind an authenticating proxy.
      '';
    };

    scanInterval = lib.mkOption {
      type = lib.types.ints.positive;
      default = 120;
      description = "Seconds between background rescans. Requests are served from cache.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cli ];

    systemd.services.codedash = {
      description = "codedash git checkout dashboard";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      # git is found through this PATH; the interpreter is absolute.
      path = [
        pkgs.git
        python
      ];
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        ExecStart =
          "${python}/bin/python3 ${./codedash.py} serve"
          + " --root ${cfg.root}"
          + " --port ${toString cfg.port}"
          + " --state-dir /var/lib/codedash"
          + " --repos-file ${reposFile}"
          + " --interval ${toString cfg.scanInterval}"
          + lib.optionalString cfg.allowActions " --allow-actions";
        Restart = "on-failure";
        RestartSec = 5;

        StateDirectory = "codedash";
        StateDirectoryMode = "0750";

        # The scan reads every checkout; fetch/fast-forward/rebase write inside
        # the root, so that one subtree is the only writable part of home. The
        # rest of the filesystem stays read-only, and no capabilities are kept.
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        ReadWritePaths = [ cfg.root ];
        PrivateTmp = true;
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        RestrictSUIDSGID = true;
        LockPersonality = true;
        ProtectKernelTunables = true;
        ProtectControlGroups = true;
      };
    };
  };
}
