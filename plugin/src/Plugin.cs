using System;
using System.IO;
using System.Reflection;
using System.Text;
using BepInEx;
using BepInEx.Configuration;
using UnityEngine;

namespace Valhallowed.Plugin
{
    // The VHD plugin, loaded into each game runtime by BepInEx. It binds no
    // Harmony patch, so a game patch that renames a method cannot break it at
    // load. Once the server's world has loaded, it writes the world's object
    // count and player count to the status file the runtime's supervisor reads:
    // that count is world truth.
    [BepInPlugin(Guid, Name, Version)]
    public sealed class Plugin : BaseUnityPlugin
    {
        public const string Guid = "com.valhallowed.plugin";
        public const string Name = "Valhallowed";

        // BepInEx needs a constant version. Releases are tags promoted without a
        // rebuild (G12), so the plugin knows its commit, never its release.
        public const string Version = "0.0.0";

        private ConfigEntry<string> _statusPath;
        private ConfigEntry<int> _statusIntervalSeconds;
        private bool _worldLoaded;
        private bool _statusFailing;
        private float _nextStatusAt;

        private void Awake()
        {
            // The supervisor generates this config from the runtime's generation.
            _statusPath = Config.Bind("Status", "Path", "",
                "Where to write the world status file the runtime's supervisor reads. Empty writes none.");
            _statusIntervalSeconds = Config.Bind("Status", "IntervalSeconds", 5,
                "How often to rewrite the world status file, in seconds.");

            string build = typeof(Plugin).Assembly
                .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "unknown";
            Logger.LogInfo($"build {build} loaded");
        }

        private void Update()
        {
            ZNet net = ZNet.instance;
            ZDOMan objects = ZDOMan.instance;
            if (net == null || objects == null || !net.IsServer())
            {
                return;
            }

            if (!_worldLoaded)
            {
                _worldLoaded = true;
                Logger.LogInfo($"world {net.GetWorldName()} loaded with {objects.NrOfObjects()} objects");
            }

            if (Time.unscaledTime < _nextStatusAt)
            {
                return;
            }
            _nextStatusAt = Time.unscaledTime + Math.Max(1, _statusIntervalSeconds.Value);
            WriteStatus(net.GetWorldName(), objects.NrOfObjects(), net.GetPeers().Count);
        }

        private void WriteStatus(string world, int objectCount, int players)
        {
            string path = _statusPath.Value;
            if (string.IsNullOrEmpty(path))
            {
                return;
            }

            string json = $"{{\"world\":{Quote(world)},\"objects\":{objectCount},\"players\":{players}}}";
            string temporary = path + ".tmp";
            try
            {
                // Written whole, then moved into place, so the supervisor never
                // reads half a report.
                File.WriteAllText(temporary, json);
                if (File.Exists(path))
                {
                    File.Replace(temporary, path, null);
                }
                else
                {
                    File.Move(temporary, path);
                }
                _statusFailing = false;
            }
            catch (Exception error) when (error is IOException || error is UnauthorizedAccessException)
            {
                // An observation fails soft: the world keeps running, and the
                // failure is logged once, not on every interval.
                if (!_statusFailing)
                {
                    Logger.LogWarning($"cannot write the world status to {path}: {error.Message}");
                }
                _statusFailing = true;
            }
        }

        private static string Quote(string value)
        {
            var quoted = new StringBuilder("\"");
            foreach (char c in value)
            {
                switch (c)
                {
                    case '"': quoted.Append("\\\""); break;
                    case '\\': quoted.Append("\\\\"); break;
                    default:
                        if (c < ' ')
                        {
                            quoted.Append($"\\u{(int)c:x4}");
                        }
                        else
                        {
                            quoted.Append(c);
                        }
                        break;
                }
            }
            return quoted.Append('"').ToString();
        }
    }
}
