using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Valhallowed.Plugin
{
    // B2's JSON, written by hand: the plugin may reference no JSON library
    // (refcheck). Each event is written once, on the game thread, as the JSON
    // object it will be in the batch; contracts/intake.events.schema.json is
    // the contract, and the plugin's test holds this writer to its example.
    internal static class Batch
    {
        public const int Contract = 1;
        // B2's maxItems.
        public const int MaxEvents = 500;
        // B2's coordinate bounds, in world units.
        public const float MaxCoordinate = 100000f;

        public static string SessionStart(DateTime at) =>
            $"{{\"kind\":\"session.start\",\"at\":{Time(at)}}}";

        public static string Join(DateTime at, string player, string name) =>
            $"{{\"kind\":\"player.join\",\"at\":{Time(at)},\"player\":{{\"id\":{Quote(player)},\"name\":{Quote(name)}}}}}";

        public static string Leave(DateTime at, string player) =>
            $"{{\"kind\":\"player.leave\",\"at\":{Time(at)},\"player\":{{\"id\":{Quote(player)}}}}}";

        public static string Position(DateTime at, string player, float x, float y, float z) =>
            $"{{\"kind\":\"player.position\",\"at\":{Time(at)},\"player\":{{\"id\":{Quote(player)}}}," +
            $"\"x\":{Number(x)},\"y\":{Number(y)},\"z\":{Number(z)}}}";

        // NaN and infinity have no JSON form, and B2 bounds the rest.
        public static bool IsCoordinate(float value) =>
            !float.IsNaN(value) && value >= -MaxCoordinate && value <= MaxCoordinate;

        public static string Envelope(string session, DateTime sentAt, IReadOnlyList<string> events)
        {
            var batch = new StringBuilder();
            batch.Append($"{{\"contract\":{Contract},\"session\":{Quote(session)},\"sent_at\":{Time(sentAt)},\"events\":[");
            for (int i = 0; i < events.Count; i++)
            {
                if (i > 0)
                {
                    batch.Append(',');
                }
                batch.Append(events[i]);
            }
            return batch.Append("]}").ToString();
        }

        public static string Quote(string value)
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

        // UTC to the microsecond, B2's time.
        private static string Time(DateTime at) =>
            "\"" + at.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'", CultureInfo.InvariantCulture) + "\"";

        private static string Number(float value) => value.ToString("R", CultureInfo.InvariantCulture);
    }
}
