#include <Geode/Geode.hpp>
#include <Geode/modify/PlayLayer.hpp>
#include <Geode/utils/web.hpp>

using namespace geode::prelude;

// Keeps the request to the bot alive until it finishes
static async::TaskHolder<web::WebResponse> s_post;

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

static void announce(GJGameLevel* level) {
	auto key = Mod::get()->getSettingValue<std::string>("bot-key");
	auto url = Mod::get()->getSettingValue<std::string>("bot-url");
	if (key.empty()) return;
	// A broken or empty address (like "httplocalhost8080") falls back to the bot on this PC
	if (url.find("://") == std::string::npos) url = "http://localhost:8080";
	if (url.ends_with('/')) url.pop_back();

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
	s_post.spawn("Demon Announcer", req.post(url + "/api/beat"), [](web::WebResponse res) {
		if (res.ok()) {
			Notification::create("Demon sent to Discord!", NotificationIcon::Success)->show();
		} else if (res.code() == 401) {
			Notification::create("Bot Key is wrong. Run /link again", NotificationIcon::Error)->show();
		} else {
			// Show the bot's own reason when it gives one
			auto reason = res.json().unwrapOr(matjson::Value())["error"].asString().unwrapOr("");
			log::warn("Demon Announcer bot failed with code {}: {}", res.code(), reason);
			Notification::create(
				reason.empty() ? "Couldn't reach the Discord bot" : "Discord bot: " + reason,
				NotificationIcon::Error
			)->show();
		}
	});
}

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
