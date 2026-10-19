// hstracker-helper: reads and writes HSTracker's Realm database on behalf of
// hs_crosssave.py. The database is opened in dynamic mode (schema read from the
// file itself), so this tool does not need HSTracker's model classes and never
// changes the schema.
//
//   hstracker-helper export <hstracker.realm>
//       Prints {"decks": [...], "games": [...]} as JSON on stdout.
//   hstracker-helper import <hstracker.realm> <games.json>
//       Inserts the games (and any new decks) described in games.json.

import Foundation
import Realm
import Realm.Dynamic
import Realm.Private

struct HelperError: Error, CustomStringConvertible {
    let description: String
}

func openRealm(_ path: String) throws -> RLMRealm {
    guard FileManager.default.fileExists(atPath: path) else {
        throw HelperError(description: "Realm file not found: \(path)")
    }
    let cfg = RLMRealmConfiguration()
    cfg.fileURL = URL(fileURLWithPath: path)
    // Use the schema stored in the file instead of compiled model classes.
    cfg.dynamic = true
    return try RLMRealm(configuration: cfg)
}

func cardsDict(_ list: RLMArray<RLMObject>) -> [String: Int] {
    var out: [String: Int] = [:]
    for i in 0..<list.count {
        let c = list.object(at: i)
        let id = c["id"] as? String ?? ""
        out[id, default: 0] += (c["count"] as? Int) ?? 0
    }
    return out
}

func export(_ realm: RLMRealm) throws {
    var decks: [[String: Any]] = []
    var games: [[String: Any]] = []
    let allDecks = realm.allObjects("Deck")
    for i in 0..<allDecks.count {
        let d = allDecks.object(at: i)
        let deckId = d["deckId"] as? String ?? ""
        decks.append([
            "deckId": deckId,
            "name": d["name"] as? String ?? "",
            "playerClass": d["_playerClass"] as? String ?? "",
            "isArena": d["isArena"] as? Bool ?? false,
            "isActive": d["isActive"] as? Bool ?? false,
            "cards": cardsDict(d["cards"] as! RLMArray<RLMObject>),
        ])
        let stats = d["gameStats"] as! RLMArray<RLMObject>
        for j in 0..<stats.count {
            let g = stats.object(at: j)
            games.append([
                "deckId": deckId,
                "hsReplayId": g["hsReplayId"] as? String ?? NSNull(),
                "startTime": (g["startTime"] as? Date)?.timeIntervalSince1970 ?? 0,
                "opponentHero": g["_opponentHero"] as? String ?? "",
            ])
        }
    }
    let data = try JSONSerialization.data(withJSONObject: ["decks": decks, "games": games],
                                          options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
}

// RealmCard is an embedded object, so cards are passed as plain values and
// created together with their parent deck.
func cardValues(_ cards: [String: Int]) -> [[String: Any]] {
    cards.keys.sorted().map { ["id": $0, "count": cards[$0]!] }
}

func importGames(_ realm: RLMRealm, _ jsonPath: String) throws {
    let data = try Data(contentsOf: URL(fileURLWithPath: jsonPath))
    guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw HelperError(description: "Invalid import file")
    }
    let newDecks = root["newDecks"] as? [[String: Any]] ?? []
    let games = root["games"] as? [[String: Any]] ?? []

    realm.beginWriteTransaction()
    do {
        for nd in newDecks {
            let now = Date()
            realm.createObject("Deck", withValue: [
                "deckId": nd["deckId"] as! String,
                "name": nd["name"] as! String,
                "_playerClass": nd["playerClass"] as! String,
                "heroId": nd["heroId"] as? String ?? "",
                "deckMajorVersion": 1,
                "deckMinorVersion": 0,
                "creationDate": now,
                "lastEdited": now,
                "isActive": true,
                "isArena": nd["isArena"] as? Bool ?? false,
                "isDungeon": false,
                "isDuels": false,
                "cards": cardValues(nd["cards"] as? [String: Int] ?? [:]),
            ])
        }

        for g in games {
            let deckId = g["deckId"] as! String
            guard let deck = realm.object(withClassName: "Deck", forPrimaryKey: deckId) else {
                throw HelperError(description: "Deck \(deckId) not found")
            }
            var value: [String: Any] = [
                "statId": UUID().uuidString,
                "_playerHero": g["playerHero"] as! String,
                "_opponentHero": g["opponentHero"] as! String,
                "coin": g["coin"] as! Bool,
                "_gameMode": g["gameMode"] as! Int,
                "_result": g["result"] as! Int,
                "turns": g["turns"] as! Int,
                "startTime": Date(timeIntervalSince1970: g["startTime"] as! Double),
                "endTime": Date(timeIntervalSince1970: g["endTime"] as! Double),
                "note": "",
                "playerName": g["playerName"] as? String ?? "",
                "opponentName": g["opponentName"] as? String ?? "",
                "wasConceded": g["wasConceded"] as? Bool ?? false,
                "rank": 0,
                "stars": 0,
                "legendRank": g["legendRank"] as? Int ?? 0,
                "opponentLegendRank": g["opponentLegendRank"] as? Int ?? 0,
                "opponentRank": 0,
                "playerCardbackId": 0,
                "opponentCardbackId": 0,
                "friendlyPlayerId": g["friendlyPlayerId"] as? Int ?? 0,
                "scenarioId": g["scenarioId"] as? Int ?? 0,
                "season": g["season"] as? Int ?? 0,
                "_gameType": g["gameType"] as! Int,
                "brawlSeasonId": g["brawlSeasonId"] as? Int ?? 0,
                "rankedSeasonId": g["rankedSeasonId"] as? Int ?? 0,
                "arenaWins": g["arenaWins"] as? Int ?? 0,
                "arenaLosses": g["arenaLosses"] as? Int ?? 0,
                "brawlWins": 0,
                "brawlLosses": 0,
                "hsReplayId": g["hsReplayId"] as! String,
            ]
            if let build = g["hearthstoneBuild"] as? Int { value["hearthstoneBuild"] = build }
            if let format = g["format"] as? String { value["__format"] = format }
            // GameStats is embedded in its deck, so it is created by appending
            // the value to the deck's list.
            (deck["gameStats"] as! RLMArray<AnyObject>).add(value as NSDictionary)
        }
        try realm.commitWriteTransaction()
    } catch {
        realm.cancelWriteTransaction()
        throw error
    }
    print("Imported \(games.count) game(s), created \(newDecks.count) deck(s).")
}

let args = CommandLine.arguments
do {
    switch (args.count, args.count > 1 ? args[1] : "") {
    case (3, "export"):
        try export(openRealm(args[2]))
    case (4, "import"):
        try importGames(openRealm(args[2]), args[3])
    default:
        throw HelperError(description: "usage: hstracker-helper export <realm> | import <realm> <games.json>")
    }
} catch {
    FileHandle.standardError.write("error: \(error)\n".data(using: .utf8)!)
    exit(1)
}
