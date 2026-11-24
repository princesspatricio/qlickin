# QlickIn (Navy & Gold edition) - Step-by-step guide

What is new in this version
- Navy / royal-blue / gold colour theme taken from The Nobles design (light and dark mode both updated).
- Quizzes are **checked automatically the moment the student presses Submit** and the score is **recorded** in Records.
- The student gets a **result email through Resend** (the page never waits for the email - it is sent in the background).
- New **Quiz results** page for the teacher: scores, %, passed / needs improvement, time taken, email status,
  every student's answers, **CSV download for Excel**, and **Allow retake**.
- New Settings: email the student, email the teacher, and the passing percentage (default 75%).
- Plain server-rendered HTML + one small CSS file - no heavy front-end framework, so pages load fast.

---------------------------------------------------------------------------------------------------

## PART 0 - Protect your Resend key (do this first)
You pasted your Resend key in a chat, so treat it as exposed.
1. Go to https://resend.com/api-keys, create a **new** key, and delete the old one.
2. The key goes ONLY in the `.env` file (on your computer) or in Render's Environment settings.
   Never put it in app.py, HTML files, GitHub, or screenshots. (`.env` is already in `.gitignore`.)

## PART 1 - Run it on your computer
1. Install Python 3.11 or newer from https://python.org (tick "Add Python to PATH").
2. Unzip the project and open a terminal inside the `qlickin` folder.
3. Create and activate a virtual environment:
   - Windows:  `python -m venv venv`  then  `venv\Scripts\activate`
   - Mac/Linux: `python3 -m venv venv`  then  `source venv/bin/activate`
4. Install the packages:  `pip install -r requirements.txt`
5. Firebase (database):
   1. https://console.firebase.google.com -> your project -> Build -> Firestore Database -> Create database.
   2. Project settings (gear) -> Service accounts -> **Generate new private key**.
   3. Save the downloaded file as `serviceAccountKey.json` inside the `qlickin` folder.
   4. (Optional) paste `firestore.rules` into Firestore -> Rules. The server uses the Admin key, so clients stay blocked.
6. Copy `.env.example` to a new file named `.env` and fill it in:
   ```
   SECRET_KEY=any-long-random-text
   FIREBASE_CREDENTIALS=serviceAccountKey.json
   BASE_URL=http://localhost:5000
   TIMEZONE=Asia/Manila
   RESEND_API_KEY=re_your_NEW_key_here
   MAIL_FROM=QlickIn <noreply@yourdomain.com>
   ```
7. Create the teacher account (one time only):
   `python app.py create-teacher "Your Name" you@gmail.com YourPassword`
8. Start the app:  `python app.py`  and open http://localhost:5000

## PART 2 - Set up Resend so student emails really arrive
Without a verified domain, Resend only delivers to **your own Resend account email** (testing mode).
To email students:
1. https://resend.com/domains -> **Add Domain** (a domain you own, e.g. yourschool.com).
2. Add the DNS records Resend shows you at your domain provider, then press **Verify**.
3. In `.env` set `MAIL_FROM=QlickIn <noreply@yourschool.com>` (must use that verified domain).
4. Restart the app. Test: Teacher -> Registrations -> approve a student, or finish a quiz as a test student.
5. If an email fails, check the terminal for a line starting with `Resend error` - it states the reason
   (invalid key, domain not verified, etc.).
Leave `RESEND_API_KEY` blank and the app just prints emails in the terminal and saves `email_preview.html`.

## PART 3 - Using the automatic quiz checking
Teacher
1. Learning Materials -> upload a PDF / Word / PowerPoint / TXT file.
2. Click **Make quiz** on the file -> choose types, number of items, points, difficulty -> Generate.
3. Review / edit the questions and answers, then under **Publish to students** choose the class,
   dates, optional time limit, and press **Publish quiz**.
4. Settings -> choose whether the student and/or you get emailed after each quiz, and set the passing %.
Student
5. Activities -> **Take quiz** -> Start -> answer -> Submit (it auto-submits when the timer ends).
6. The student instantly sees score, %, remarks and (if allowed) the corrected answers, and gets an email.
Teacher results
7. Learning Materials -> open the quiz -> **Quiz results**.
   - Table of every student: score, %, Passed / Needs improvement, time taken, email status.
   - **Answers** = what that student answered. **Allow retake** = erases the attempt so they can try again.
   - **Download results** = CSV that opens in Excel.
8. Scores also appear automatically in **Records** and on each student's page.
Typing mistakes are forgiven on identification / fill-in-the-blank answers (small spelling errors still count).

## PART 4 - Put it online (Render.com)
1. Push the project to a **private** GitHub repo (make sure `.env` and `serviceAccountKey.json` are NOT included).
2. Render -> New -> Web Service -> pick the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
3. Environment tab -> add:
   `SECRET_KEY`, `BASE_URL` (your Render URL, e.g. https://qlickin.onrender.com), `TIMEZONE`,
   `RESEND_API_KEY`, `MAIL_FROM`, and `FIREBASE_CREDENTIALS_JSON` (paste the WHOLE JSON of the Firebase key).
4. Deploy. Create the teacher account once from Render's Shell tab:
   `python app.py create-teacher "Your Name" you@gmail.com YourPassword`

## Troubleshooting
- "STARTUP ERROR: could not connect to Firebase" -> wrong or missing Firebase key file / JSON.
- Emails not arriving -> domain not verified, or `MAIL_FROM` does not use the verified domain; see `Resend error` in the logs.
- Student closed the browser mid-quiz -> Quiz results shows "Not submitted"; press **Allow retake**.
- Result shows "Sending..." in the Email column -> refresh in a few seconds.
