using System.Reflection;
using BepInEx;

namespace Valhallowed.Plugin
{
    // The VHD plugin, loaded into each game runtime by BepInEx. It binds no
    // Harmony patch, so a game patch that renames a method cannot break it at
    // load. For now it reports once that the server's world has loaded, with its
    // object count.
    [BepInPlugin(Guid, Name, Version)]
    public sealed class Plugin : BaseUnityPlugin
    {
        public const string Guid = "com.valhallowed.plugin";
        public const string Name = "Valhallowed";

        // BepInEx needs a constant version. Releases are tags promoted without a
        // rebuild (G12), so the plugin knows its commit, never its release.
        public const string Version = "0.0.0";

        private bool _worldReported;

        private void Awake()
        {
            string build = typeof(Plugin).Assembly
                .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "unknown";
            Logger.LogInfo($"build {build} loaded");
        }

        private void Update()
        {
            if (_worldReported)
            {
                return;
            }

            ZNet net = ZNet.instance;
            ZDOMan objects = ZDOMan.instance;
            if (net == null || objects == null || !net.IsServer())
            {
                return;
            }

            _worldReported = true;
            Logger.LogInfo($"world {net.GetWorldName()} loaded with {objects.NrOfObjects()} objects");
        }
    }
}
