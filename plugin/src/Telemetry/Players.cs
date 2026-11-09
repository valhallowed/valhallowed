namespace Valhallowed.Plugin
{
    // A player as B2 names them. The id is the string the game's own
    // PlatformUserID prints for a Steamworks peer (ZNet.GetPlayerList builds it
    // from the socket's host name, the Steam id). VHD starts the server without
    // -crossplay, so every peer is a Steamworks peer.
    internal static class Players
    {
        // B2's player_id and name lengths.
        public const int MaxId = 64;
        public const int MaxName = 64;

        // The player's id, or null when it would break B2's player_id pattern,
        // ^[A-Za-z0-9_.:-]{1,64}$.
        public static string SteamId(string hostName)
        {
            string id = "Steam_" + hostName;
            if (string.IsNullOrEmpty(hostName) || id.Length > MaxId)
            {
                return null;
            }
            foreach (char c in id)
            {
                bool allowed = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
                    || c == '_' || c == '.' || c == ':' || c == '-';
                if (!allowed)
                {
                    return null;
                }
            }
            return id;
        }

        // The client chooses the name, so it is made to fit B2: control
        // characters removed, cut to 64 characters without splitting a
        // surrogate pair. Null when nothing is left.
        public static string Name(string name)
        {
            if (name == null)
            {
                return null;
            }
            var kept = new System.Text.StringBuilder(name.Length);
            foreach (char c in name)
            {
                if (c >= ' ' && c != '\u007f')
                {
                    kept.Append(c);
                }
            }
            int length = 0;
            int characters = 0;
            while (length < kept.Length && characters < MaxName)
            {
                length += char.IsHighSurrogate(kept[length]) && length + 1 < kept.Length ? 2 : 1;
                characters++;
            }
            return length == 0 ? null : kept.ToString(0, length);
        }
    }
}
