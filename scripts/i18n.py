"""Writes translations/{en,bg}.json from one place.

Run: python scripts/i18n.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "tapechart"

ROLE_NAMES = {
    "en": {
        "presence": "Presence",
        "keycard": "Key card",
        "door": "Door",
        "window": "Window",
        "dnd": "Do not disturb",
        "mur": "Make up room",
        "climate": "Thermostat",
        "temperature": "Room temperature",
        "online": "Controller online",
        "alarm": "Alarm",
        "scenario_welcome": "Scenario: welcome",
        "scenario_eco": "Scenario: eco",
        "scenario_off": "Scenario: all off",
    },
    "bg": {
        "presence": "Присъствие",
        "keycard": "Картов ключ",
        "door": "Врата",
        "window": "Прозорец",
        "dnd": "Не безпокойте",
        "mur": "Почистете стаята",
        "climate": "Термостат",
        "temperature": "Температура в стаята",
        "online": "Контролерът е онлайн",
        "alarm": "Аларма",
        "scenario_welcome": "Сценарий: посрещане",
        "scenario_eco": "Сценарий: икономичен",
        "scenario_off": "Сценарий: изключи всичко",
    },
}

ROLE_HELP = {
    "en": {
        "presence": "Someone is physically in the room (motion, presence). binary_sensor or input_boolean.",
        "keycard": "A key card is in the holder.",
        "door": "On = the door is open.",
        "window": "On = the window is open.",
        "dnd": "The guest asked not to be disturbed.",
        "mur": "The guest asked for the room to be cleaned.",
        "climate": "Mode, set and current temperature come from it.",
        "temperature": "Only if the room temperature does not come from the thermostat.",
        "online": "The room controller is reachable (connectivity).",
        "alarm": "Leak, smoke, SOS. Its device class is sent too.",
        "scenario_welcome": "The script or scene the PMS may run to prepare the room. Nothing else can be run from the PMS.",
        "scenario_eco": "The script or scene for the eco mode.",
        "scenario_off": "The script or scene that switches the room off.",
    },
    "bg": {
        "presence": "Някой е физически в стаята (движение, присъствие). binary_sensor или input_boolean.",
        "keycard": "Картата е в държача.",
        "door": "Включено = вратата е отворена.",
        "window": "Включено = прозорецът е отворен.",
        "dnd": "Гостът е помолил да не го безпокоят.",
        "mur": "Гостът е поискал почистване на стаята.",
        "climate": "От него идват режимът, зададената и текущата температура.",
        "temperature": "Само ако температурата не идва от термостата.",
        "online": "Контролерът на стаята е достъпен (connectivity).",
        "alarm": "Теч, дим, SOS. Изпраща се и видът на сензора.",
        "scenario_welcome": "Скриптът или сцената, които PMS-ът може да пусне, за да подготви стаята. Нищо друго не може да се пуска от PMS-а.",
        "scenario_eco": "Скриптът или сцената за икономичния режим.",
        "scenario_off": "Скриптът или сцената, които изключват стаята.",
    },
}

ONLINE_DEVICE = {
    "en": (
        "Online = the device is available",
        "For devices without a connectivity entity: the room is offline when every entity of the chosen device is unavailable.",
    ),
    "bg": (
        "Онлайн = устройството е достъпно",
        "За устройства без собствен сензор за връзка: стаята е офлайн, когато всички entities на избраното устройство са недостъпни.",
    ),
}

T = {
    "en": {
        "config": {
            "step": {
                "user": {
                    "title": "Connect to Tape Chart",
                    "description": "In Tape Chart open **Settings → Integrations → Home Assistant**, pick the property and press **Generate pairing code**. Type the PMS address shown there and the code. The code is valid for 10 minutes.",
                    "data": {"pms_url": "PMS address", "code": "Pairing code"},
                    "data_description": {
                        "pms_url": "For example https://pms.example.com or http://192.168.1.20:3000",
                        "code": "Eight letters and digits, like ABCD-EFGH",
                    },
                },
                "reauth_confirm": {
                    "title": "Pair {property} again",
                    "description": "Tape Chart no longer accepts this Home Assistant for {property}. Generate a new pairing code for the same property in Tape Chart and type it here. The room mapping is kept.",
                    "data": {"pms_url": "PMS address", "code": "Pairing code"},
                },
            },
            "error": {
                "invalid_url": "This is not a valid address. Start it with http:// or https://.",
                "insecure_url": "Use https:// for an address on the internet. Plain http:// is accepted only for an address in the local network.",
                "invalid_code": "The pairing code is not valid or was already used.",
                "code_expired": "The pairing code has expired. Generate a new one.",
                "unreachable": "Tape Chart could not reach this Home Assistant. Check the internal or external URL under Settings → System → Network, or enable Home Assistant Cloud.",
                "rate_limited": "Too many attempts. Wait a minute and try again.",
                "cannot_connect": "Could not connect to the PMS. Check the address.",
                "no_ha_url": "Home Assistant has no URL configured. Set one under Settings → System → Network.",
                "unknown": "Unexpected error",
            },
            "abort": {
                "already_configured": "This property is already paired.",
                "repaired": "This property was already paired. Its connection has been updated to the new pairing.",
                "other_property_updated": "That code was for {property}; its connection has been updated. To fix this property, generate a code for this property and try again.",
                "reauth_successful": "Paired again.",
                "wrong_property": "That code is for another property. Generate the code for the same property.",
            },
        },
        "options": {
            "step": {
                "init": {
                    "title": "Tape Chart",
                    "menu_options": {
                        "map_room": "Map a room",
                        "fill_pattern": "Fill other rooms by pattern",
                        "settings": "Settings",
                    },
                },
                "map_room": {
                    "title": "Map a room",
                    "description": "Choose the room to map.",
                    "data": {"room": "Room"},
                },
                "map_device": {
                    "title": "Device of room {room}",
                    "description": "If one device serves this room (for example an ESPHome room controller), choose it: the lists on the next step will show its entities first, fill by pattern can find the same entities in the other rooms' devices, and the room can be reported offline when the device is. Leave it empty otherwise.",
                    "data": {
                        "device": "Device of the room (optional)",
                        "only_device": "List only this device's entities",
                    },
                    "data_description": {
                        "only_device": "Turn off to pick an entity of another device (for example a door contact) for some role.",
                    },
                },
                "map_entities": {
                    "title": "Room {room}",
                    "description": "Choose which entity plays which role. Only the entities chosen here are ever sent to Tape Chart. Leave a role empty if the room has nothing for it.",
                    "data": "ROLES",
                    "data_description": "ROLE_HELP",
                },
                "fill_pattern": {
                    "title": "Fill by pattern",
                    "description": 'Choose a room that is already mapped. For every other room the same entities are looked up: first inside that room\'s device (found by the room number in the device name, like "Room 206"), then by putting the room number into each entity id (room_205_pir → room_206_pir). Only entities that exist are proposed, and you will see them, and what was not found, before anything is saved.',
                    "data": {
                        "source": "Mapped room to copy",
                        "overwrite": "Replace roles that are already mapped",
                    },
                },
                "fill_confirm": {
                    "title": "Found entities for {rooms} rooms",
                    "description": "{result}\n\nSave these mappings?",
                },
                "settings": {
                    "title": "Settings",
                    "data": {
                        "presence_hold_minutes": "Presence off delay",
                        "temperature_min_delta": "Smallest temperature change to send",
                        "temperature_min_interval": "Shortest time between temperature updates",
                    },
                    "data_description": {
                        "presence_hold_minutes": "Presence 'on' is sent at once; 'off' only when it lasts this long.",
                        "temperature_min_delta": "Smaller changes are not sent.",
                        "temperature_min_interval": "Changes coming faster are combined.",
                    },
                },
            },
            "error": {
                "nothing_found": "No matching entities were found for the other rooms."
            },
            "abort": {
                "not_loaded": "The integration is not running. Check the connection to Tape Chart first.",
                "no_mapped_rooms": "Map one room first, then fill the others from it.",
            },
        },
        "entity": {
            "binary_sensor": {"guest_checked_in": {"name": "Guest checked in"}},
            "event": {
                "stay": {
                    "name": "Stay",
                    "state_attributes": {
                        "event_type": {
                            "state": {
                                "checked_in": "Checked in",
                                "checked_out": "Checked out",
                                "moved_in": "Moved in",
                                "moved_out": "Moved out",
                                "stay_changed": "Stay changed",
                                "cancelled": "Cancelled",
                            }
                        },
                    },
                }
            },
            "sensor": {
                "status": {
                    "name": "Status",
                    "state": {
                        "vacant": "Vacant",
                        "arriving": "Arriving",
                        "occupied": "Occupied",
                        "departing": "Departing",
                        "departed": "Departed",
                        "out_of_service": "Out of service",
                    },
                    "state_attributes": {
                        "business_date": {"name": "Business date"},
                        "guest": {"name": "Guest"},
                        "companions": {"name": "Companions"},
                    },
                },
                "guest_language": {"name": "Guest language"},
                "adults": {"name": "Adults"},
                "children": {
                    "name": "Children",
                    "state_attributes": {"ages": {"name": "Ages"}},
                },
                "check_in": {"name": "Check-in"},
                "check_out": {"name": "Check-out"},
                "nights": {"name": "Nights"},
                "nights_left": {"name": "Nights left"},
                "next_arrival": {
                    "name": "Next arrival",
                    "state_attributes": {
                        "adults": {"name": "Adults"},
                        "children": {"name": "Children"},
                    },
                },
            },
        },
        "exceptions": {
            "auth_failed": "Tape Chart refused this Home Assistant. Pair it again with a new code.",
            "cannot_connect": "Could not reach Tape Chart ({error}).",
        },
        "issues": {
            "connection_lost": {
                "title": "Tape Chart: {property} is disconnected",
                "description": "Tape Chart no longer accepts this Home Assistant for {property}, so room data and signals are not exchanged. Generate a new pairing code in Tape Chart (Settings → Integrations → Home Assistant) and open the Tape Chart integration here to pair again.",
            }
        },
    },
    "bg": {
        "config": {
            "step": {
                "user": {
                    "title": "Свързване с Tape Chart",
                    "description": "В Tape Chart отворете **Настройки → Интеграции → Home Assistant**, изберете обекта и натиснете **Създай код за сдвояване**. Въведете адреса на PMS-а, който е показан там, и кода. Кодът важи 10 минути.",
                    "data": {"pms_url": "Адрес на PMS-а", "code": "Код за сдвояване"},
                    "data_description": {
                        "pms_url": "Например https://pms.example.com или http://192.168.1.20:3000",
                        "code": "Осем букви и цифри, например ABCD-EFGH",
                    },
                },
                "reauth_confirm": {
                    "title": "Ново сдвояване за {property}",
                    "description": "Tape Chart вече не приема този Home Assistant за {property}. Създайте нов код за същия обект в Tape Chart и го въведете тук. Връзките на стаите се запазват.",
                    "data": {"pms_url": "Адрес на PMS-а", "code": "Код за сдвояване"},
                },
            },
            "error": {
                "invalid_url": "Адресът не е правилен. Трябва да започва с http:// или https://.",
                "insecure_url": "За адрес в интернет използвайте https://. Обикновено http:// се приема само за адрес в локалната мрежа.",
                "invalid_code": "Кодът не е верен или вече е използван.",
                "code_expired": "Кодът е изтекъл. Създайте нов.",
                "unreachable": "Tape Chart не може да стигне до този Home Assistant. Проверете вътрешния или външния адрес в Настройки → Система → Мрежа или включете Home Assistant Cloud.",
                "rate_limited": "Твърде много опити. Изчакайте минута и опитайте пак.",
                "cannot_connect": "Няма връзка с PMS-а. Проверете адреса.",
                "no_ha_url": "Home Assistant няма зададен адрес. Задайте го в Настройки → Система → Мрежа.",
                "unknown": "Неочаквана грешка",
            },
            "abort": {
                "already_configured": "Този обект вече е сдвоен.",
                "repaired": "Този обект вече беше сдвоен. Връзката му е обновена с новото сдвояване.",
                "other_property_updated": "Кодът беше за {property} и неговата връзка е обновена. За да оправите този обект, създайте код за него и опитайте пак.",
                "reauth_successful": "Сдвояването е готово.",
                "wrong_property": "Кодът е за друг обект. Създайте код за същия обект.",
            },
        },
        "options": {
            "step": {
                "init": {
                    "title": "Tape Chart",
                    "menu_options": {
                        "map_room": "Свържи стая",
                        "fill_pattern": "Попълни другите стаи по шаблон",
                        "settings": "Настройки",
                    },
                },
                "map_room": {
                    "title": "Свържи стая",
                    "description": "Изберете стаята.",
                    "data": {"room": "Стая"},
                },
                "map_device": {
                    "title": "Устройство на стая {room}",
                    "description": "Ако стаята се обслужва от едно устройство (например контролер на стаята с ESPHome), изберете го: на следващата стъпка списъците ще показват неговите entities, попълването по шаблон ще намира същите entities в устройствата на другите стаи, а стаята ще се показва офлайн, когато устройството е недостъпно. Иначе оставете празно.",
                    "data": {
                        "device": "Устройство на стаята (по избор)",
                        "only_device": "Показвай само entities на това устройство",
                    },
                    "data_description": {
                        "only_device": "Изключете, за да изберете за някоя роля entity от друго устройство (например контакт на вратата).",
                    },
                },
                "map_entities": {
                    "title": "Стая {room}",
                    "description": "Изберете кое entity за какво отговаря. Към Tape Chart се изпращат само избраните тук. Оставете празно, ако стаята няма нищо за тази роля.",
                    "data": "ROLES",
                    "data_description": "ROLE_HELP",
                },
                "fill_pattern": {
                    "title": "Попълване по шаблон",
                    "description": "Изберете стая, която вече е свързана. За всяка друга стая се търсят същите entities: първо в устройството на стаята (намира се по номера в името, например „Room 206“), после като номерът на стаята се сложи във всяко entity id (room_205_pir → room_206_pir). Предлагат се само entities, които съществуват, и ще видите какво е намерено и какво не, преди да се запише нещо.",
                    "data": {
                        "source": "Свързана стая за копиране",
                        "overwrite": "Замени и вече свързаните роли",
                    },
                },
                "fill_confirm": {
                    "title": "Намерени entities за {rooms} стаи",
                    "description": "{result}\n\nДа се запишат ли тези връзки?",
                },
                "settings": {
                    "title": "Настройки",
                    "data": {
                        "presence_hold_minutes": "Изчакване преди „няма присъствие“",
                        "temperature_min_delta": "Най-малка промяна на температурата за изпращане",
                        "temperature_min_interval": "Най-кратко време между две температури",
                    },
                    "data_description": {
                        "presence_hold_minutes": "„Има присъствие“ се изпраща веднага, а „няма“ – само ако трае толкова време.",
                        "temperature_min_delta": "По-малките промени не се изпращат.",
                        "temperature_min_interval": "По-честите промени се събират в една.",
                    },
                },
            },
            "error": {
                "nothing_found": "Не са намерени подходящи entities за другите стаи."
            },
            "abort": {
                "not_loaded": "Интеграцията не работи. Първо проверете връзката с Tape Chart.",
                "no_mapped_rooms": "Първо свържете една стая, после попълнете другите от нея.",
            },
        },
        "entity": {
            "binary_sensor": {"guest_checked_in": {"name": "Настанен гост"}},
            "event": {
                "stay": {
                    "name": "Престой",
                    "state_attributes": {
                        "event_type": {
                            "state": {
                                "checked_in": "Настанен",
                                "checked_out": "Напуснал",
                                "moved_in": "Преместен в стаята",
                                "moved_out": "Преместен от стаята",
                                "stay_changed": "Променен престой",
                                "cancelled": "Анулиран",
                            }
                        },
                    },
                }
            },
            "sensor": {
                "status": {
                    "name": "Статус",
                    "state": {
                        "vacant": "Свободна",
                        "arriving": "Пристига",
                        "occupied": "Заета",
                        "departing": "Напуска днес",
                        "departed": "Освободена",
                        "out_of_service": "Извън експлоатация",
                    },
                    "state_attributes": {
                        "business_date": {"name": "Работен ден"},
                        "guest": {"name": "Гост"},
                        "companions": {"name": "Придружители"},
                    },
                },
                "guest_language": {"name": "Език на госта"},
                "adults": {"name": "Възрастни"},
                "children": {
                    "name": "Деца",
                    "state_attributes": {"ages": {"name": "Възраст"}},
                },
                "check_in": {"name": "Пристигане"},
                "check_out": {"name": "Напускане"},
                "nights": {"name": "Нощувки"},
                "nights_left": {"name": "Оставащи нощувки"},
                "next_arrival": {
                    "name": "Следващо пристигане",
                    "state_attributes": {
                        "adults": {"name": "Възрастни"},
                        "children": {"name": "Деца"},
                    },
                },
            },
        },
        "exceptions": {
            "auth_failed": "Tape Chart отказа този Home Assistant. Сдвоете го отново с нов код.",
            "cannot_connect": "Няма връзка с Tape Chart ({error}).",
        },
        "issues": {
            "connection_lost": {
                "title": "Tape Chart: {property} не е свързан",
                "description": "Tape Chart вече не приема този Home Assistant за {property} и данните за стаите не се обменят. Създайте нов код в Tape Chart (Настройки → Интеграции → Home Assistant) и отворете интеграцията Tape Chart тук, за да сдвоите отново.",
            }
        },
    },
}


def build(lang: str) -> dict:
    data = json.loads(json.dumps(T[lang]))
    step = data["options"]["step"]["map_entities"]
    step["data"] = {**ROLE_NAMES[lang], "online_from_device": ONLINE_DEVICE[lang][0]}
    step["data_description"] = {
        **ROLE_HELP[lang],
        "online_from_device": ONLINE_DEVICE[lang][1],
    }
    return data


def main() -> None:
    (ROOT / "translations").mkdir(exist_ok=True)
    for lang in ("en", "bg"):
        text = json.dumps(build(lang), ensure_ascii=False, indent=2) + "\n"
        (ROOT / "translations" / f"{lang}.json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
