<h1 align="center">🛡️ Distributed Token Bucket Rate Limiter</h1>

<p align="center">
  <em>A production-ready FastAPI rate limiting service built to handle extreme scale.</em><br/>
  <em>Think of it as the ultimate bouncer for your APIs.</em>
</p>

## 📖 What is this?

Imagine you are running an exclusive nightclub (your server). If too many people try to rush the door at once, the club gets overwhelmed and crashes. 

This project is the **Bouncer**. Before anyone (a client) is allowed to access your services, they must ask the bouncer. 
- If they are following the rules (e.g., only 5 requests per second), the bouncer says **"Yes (HTTP 200)"**.
- If they are rushing the door, the bouncer says **"No, wait! (HTTP 429 Too Many Requests)"**.

It combines a **distributed engine**, **Redis shared state**, **atomic Lua scripting**, and **Docker orchestration** into a single backend system.

## 🌟 Key Features

- **Blazing Fast:** Uses atomic Lua scripts in Redis to prevent race conditions and ensure microsecond decision times.
- **Two Modes:** Choose between `Token Bucket` (allows smooth bursts) or `Sliding Window` (hard ceilings).
- **Fully Distributed:** Scales out horizontally across multiple API instances.
- **Live Dashboard:** Watch traffic get allowed and denied in real-time.
- **Foolproof Setup:** Entirely Dockerized. Run one command and you are ready to go.

---

## 🚀 Quick Start Guide (Foolproof Setup)

We have designed this so anyone can run it in under 60 seconds.

### 1. Prerequisites
You only need two things installed on your computer:
*   [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Make sure it is running!)
*   [Git](https://git-scm.com/downloads)

### 2. Clone & Start
Open your terminal (Command Prompt, PowerShell, or Terminal) and run:

```bash
# Clone the repository
git clone https://github.com/TirrthM/token_bucket_rate_limiter.git
cd token_bucket_rate_limiter

# Start the entire cluster in the background
docker compose up --build -d
```

### 3. Verify it is running
```bash
docker compose ps
```
*You should see 5 containers running: 3 API instances (`api1`, `api2`, `api3`), 1 `redis` database, and 1 `nginx` load balancer.*

---

## 🎮 How to Experiment (No Terminal Required!)

Command-line tools like `curl` can be tricky depending on your operating system (especially on Windows PowerShell). Instead, we've built a **beautiful visual interface** for you to play with!

### Step 1: Open the Dashboard
Open your browser and keep this tab open on the side:
👉 **[http://localhost:8000/dashboard](http://localhost:8000/dashboard)**

### Step 2: Set the Rules
We need to tell the system the rules for our test client (let's call them `"checkout"`).
1. Go to our interactive API Docs: **[http://localhost:8000/docs](http://localhost:8000/docs)**
2. Click the green **`POST /admin/config`** box.
3. Click **"Try it out"**.
4. In the Request Body, paste this:
   ```json
   {
     "client_key": "checkout",
     "mode": "token_bucket",
     "requests_per_second": 5,
     "burst_size": 20
   }
   ```
5. Click the large blue **Execute** button.

### Step 3: Test the Rate Limiter!
1. On that same API Docs page, scroll up and click the green **`POST /check`** box.
2. Click **"Try it out"**.
3. In the Request Body, paste this:
   ```json
   {
     "client_key": "checkout"
   }
   ```
4. Click the blue **Execute** button. 
5. Scroll down to see the **Server response** (`200 OK`). 
6. **Now for the fun part:** Click the **Execute** button as fast as you can 20 or 30 times in a row! Eventually, the server will block you and return a `429 Too Many Requests` error. 

*(Check your Dashboard tab to see your requests being mapped and blocked in real-time!)*

---

## 🔥 Extreme Load Testing

Want to see how this system handles serious pressure? We included a script that will bombard the rate limiter with thousands of requests per second.

Run this command in your terminal:
```bash
docker compose run --rm -e BASE_URL=http://nginx api1 python scripts/load_test.py
```

**What is happening?**
The script acts like 100 users trying to attack your server all at once. The load balancer (Nginx) distributes this attack across all 3 API replicas. You will see a report proving that the system successfully blocked the exact right amount of traffic without a single error, processing over 5,000+ requests per second!

*(Don't forget to check the Dashboard and select the new `load_...` client from the dropdown to see the massive spike!)*

---

## 🧹 Clean Up

When you are finished playing, you can shut down the servers and clean up your computer by running:
```bash
docker compose down -v
```
