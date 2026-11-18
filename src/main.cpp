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

// Sends every demon already beaten before the mod was installed (once per key)
static void syncOldDemons() {
	auto key = Mod::get()->getSettingValue<std::string>("bot-key");
	if (key.empty() || Mod::get()->getSavedValue<std::string>("synced-key") == key) return;

	std::vector<matjson::Value> levels;
	auto add = [&](GJGameLevel* level) {
		if (level && level->m_stars.value() == 10 && level->m_normalPercent.value() >= 100) {
			levels.push_back(matjson::makeObject({
				{"levelId", level->m_levelID.value()},
				{"attempts", level->m_attempts.value()}
			}));
		}
	};
	auto glm = GameLevelManager::sharedState();
	CCDictElement* element;
	CCDICT_FOREACH(glm->m_onlineLevels, element) {
		add(static_cast<GJGameLevel*>(element->getObject()));
	}
	for (int id : {14, 18, 20}) add(glm->getMainLevel(id, true)); // Clubstep, ToE 2, Deadlocked

	if (levels.empty()) {
		Mod::get()->setSavedValue("synced-key", key);
		return;
	}

	web::WebRequest req;
	req.bodyJSON(matjson::makeObject({{"key", key}, {"levels", levels}}));
	s_sync.spawn("Demon Announcer sync", req.post(botUrl() + "/api/sync"), [key](web::WebResponse res) {
		if (res.ok()) {
			Mod::get()->setSavedValue("synced-key", key);
			auto count = res.json().unwrapOr(matjson::Value())["imported"].asInt().unwrapOr(0);
			if (count > 0) {
				Notification::create(fmt::format("Added {} old demons to Discord!", count), NotificationIcon::Success)->show();
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
			syncOldDemons();
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
