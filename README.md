# חדר מצב · DISPATCH LIVE

מסך שליטה ביתי בעברית: 8 ערוצי שידור חי, חדשות ממוינות, התרעות פיקוד העורף עם מפה,
מד מתיחות, רעידות אדמה, מדדים, טיסות ומפת עומסי תנועה ארצית.

רץ כולו על המחשב שלך. Python בלבד, בלי התקנת חבילות (`pip`), בלי חשבון ובלי ענן.

## הפעלה

| מערכת | פקודה |
| --- | --- |
| Windows | לחיצה כפולה על `Start-Dispatch-LIVE.bat` |
| Windows, צפייה גם מהטלפון ברשת הביתית | `Start-Dispatch-NETWORK.bat` |
| Mac / Linux | `./Start-Dispatch.sh` |
| עצירה | `Stop-Dispatch.bat` (או סגירת חלון המסך) |

נדרש Python 3.9 ומעלה. המסך נפתח בכתובת `http://localhost:3100`.

## מה רץ

| קובץ | תפקיד | פורט |
| --- | --- | --- |
| `index.html` | המסך עצמו, קובץ אחד | |
| `server.py` | מגיש את המסך ומתווך את מקורות המידע | 3100 |
| `brain.py` | שירות התנועה: מודד מול Google Routes ולומד דפוסים, בתוך המכסה החינמית | 3200 |

## מפת התנועה (אופציונלי)

בלי מפתח המערכת עובדת, רק בלי נתוני תנועה.

1. Google Cloud Console → להפעיל **Routes API** → ליצור API key ולהגביל אותו ל-Routes API.
2. לשמור את המפתח בקובץ `google.key` בתיקייה הזו (שורה אחת, בלי רווחים).

`google.key` רשום ב-`.gitignore` ולעולם לא עולה לריפו. התקציב נעצר ב-4,500 קריאות
בחודש, מתחת ל-5,000 החינמיות. פירוט: [docs/traffic-brain.txt](docs/traffic-brain.txt).

## בדיקת תקינות

| כתובת | מה מראה |
| --- | --- |
| `localhost:3100/version` | איזו גרסה מוגשת בפועל מהדיסק |
| `localhost:3100/oref/history-debug` | מה כל כתובת של פיקוד העורף מחזירה כרגע |
| `localhost:3200/traffic` | מצב התנועה, כולל `health` עם סיבת הכישלון אם גוגל לא עונה |
| `localhost:3200/model` | כיסוי המודל וניצול התקציב |

## פיתוח

```
python -m unittest discover -s tests     # בדיקות יחידה, stdlib בלבד
node tests/check_js.js                   # ה-JavaScript שבתוך index.html מתפרש
node tests/render_test.js                # בדיקת תצוגה בדפדפן (דורש playwright)
```

אותן בדיקות רצות ב-GitHub Actions על כל push.
בכל מסירה מעדכנים את מספר הגרסה בשני מקומות ב-`index.html` (`DL_BUILD` ו-`build-tag`) ושורה ב-[CHANGELOG.md](CHANGELOG.md).

התוכנית קדימה: [docs/ROADMAP.md](docs/ROADMAP.md).
