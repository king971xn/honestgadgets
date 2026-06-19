@echo off
cd /d "I:\codex所有的项目文件夹\项目五 - AI联盟营销评测网站（高客单价、小众蓝海）\Aixiangmu"
python auto_pilot.py >> auto_pilot_daily.log 2>&1
echo [%date% %time%] Daily run completed >> auto_pilot_daily.log
