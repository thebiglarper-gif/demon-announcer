#include <Geode/Geode.hpp>
#include <Geode/modify/PlayLayer.hpp>
#include <Geode/modify/MenuLayer.hpp>
#include <Geode/utils/web.hpp>

using namespace geode::prelude;

// Keep the requests to the bot alive until they finish
static async::TaskHolder<web::WebResponse> s_post;
static async::TaskHolder<web::WebResponse> s_sync;

// GD's demon difficulty numbers -> the names the bot understands
static char const* demonKey(GJGameLevel* level) {
	switch (level->m_demonDifficulty) {
		case 3: return "easy";
		case 4: return "medium";
		case 5: return "insane";
		case 6: return "extreme";
		default: return "hard";
	}
}

static std::string botUrl() {
	auto url = Mod::get()->getSettingValue<std::string>("bot-url");
	// A broken or empty address (like "httplocalhost8080") falls back to the bot on this PC
	if (url.find("://") == std::string::npos) url = "http://localhost:8080";
	if (url.ends_with('/')) url.pop_back();
	return url;
}

static std::string botError(web::WebResponse& res) {
	return res.json().unwrapOr(matjson::Value())["error"].asString().unwrapOr("");
}

static void announce(GJGameLevel* level) {
	auto key = Mod::get()->getSettingValue<std::string>("bot-key");
	if (key.empty()) return;

	auto body = matjson::makeObject({
		{"key", key},
		{"levelId", level->m_levelID.value()},
		{"levelName", std::string(level->m_levelName)},
		{"difficulty", demonKey(level)},
		{"attempts", level->m_attempts.value()},
		{"platformer", level->isPlatformer()},
		{"gdName", std::string(GameManager::sharedState()->m_playerName)}
	});

	web::WebRequest req;
	req.bodyJSON(body);
	s_post.spawn("Demon Announcer", req.post(botUrl() + "/api/beat"), [](web::WebResponse res) {
		if (res.ok()) {
			Notification::create("Demon sent to Discord!", NotificationIcon::Success)->show();
		} else if (res.code() == 401) {
			Notification::create("Bot Key is wrong. Run /link again", NotificationIcon::Error)->show();
		} else {
			auto reason = botError(res);
			log::warn("Demon Announcer bot failed with code {}: {}", res.code(), reason);
			Notification::create(
				reason.empty() ? "Couldn't reach the Discord bot" : "Discord bot: " + reason,
				NotificationIcon::Error
			)->show();
		}
	});
}

// Sends every demon in the save file, so the bot's list matches it (runs each time GD opens)
static void syncSaveFile() {
	auto key = Mod::get()->getSettingValue<std::string>("bot-key");
	if (key.empty()) return;

	std::map<int, int> demons; // level ID -> attempts
	auto add = [&](GJGameLevel* level) {
		if (level && level->m_stars.value() == 10 && level->m_normalPercent.value() >= 100) {
			demons[level->m_levelID.value()] = level->m_attempts.value();
		}
	};
	auto glm = GameLevelManager::sharedState();
	for (auto [id, level] : CCDictionaryExt<std::string, GJGameLevel*>(glm->m_onlineLevels)) {
		add(level);
	}
	for (int id : {14, 18, 20}) add(glm->getMainLevel(id, true)); // Clubstep, ToE 2, Deadlocked

	// GD's own record of completed demons ("demon_<id>"), which still has levels you deleted from Saved
	for (auto [name, value] : CCDictionaryExt<std::string, CCObject*>(GameStatsManager::sharedState()->m_completedLevels)) {
		if (!name.starts_with("demon_")) continue;
		if (auto id = utils::numFromString<int>(name.substr(6)); id.isOk() && !demons.contains(id.unwrap())) {
			demons[id.unwrap()] = 0; // attempts unknown for deleted levels
		}
	}
	if (demons.empty()) return;

	std::vector<matjson::Value> levels;
	for (auto [id, attempts] : demons) {
		levels.push_back(matjson::makeObject({{"levelId", id}, {"attempts", attempts}}));
	}
	web::WebRequest req;
	req.bodyJSON(matjson::makeObject({{"key", key}, {"levels", levels}}));
	s_sync.spawn("Demon Announcer sync", req.post(botUrl() + "/api/sync"), [](web::WebResponse res) {
		if (res.ok()) {
			auto count = res.json().unwrapOr(matjson::Value())["imported"].asInt().unwrapOr(0);
			if (count > 0) {
				Notification::create(fmt::format("Added {} demons from your save to Discord!", count), NotificationIcon::Success)->show();
			}
		} else {
			log::warn("Demon Announcer sync failed with code {}: {}", res.code(), botError(res));
		}
	});
}

class $modify(MenuLayer) {
	bool init() {
		if (!MenuLayer::init()) return false;
		static bool synced = false;
		if (!synced) {
			synced = true;
			syncSaveFile();
		}
		return true;
	}
};

class $modify(PlayLayer) {
	void levelComplete() {
		// Check these BEFORE the game saves the completion
		bool isDemon = m_level->m_stars.value() == 10;
		bool firstTime = m_level->m_normalPercent.value() < 100;
		bool realRun = !m_isPracticeMode && !m_isTestMode; // no practice, no start pos

		PlayLayer::levelComplete();

		if (isDemon && firstTime && realRun) announce(m_level);
	}
};
