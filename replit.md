# Diamond Dogs Scheduler

## Team Members
- luke.david.reimer@gmail.com
- forster.graham@gmail.com
- clockwerks77@gmail.com
- gavyn.mcleod@gmail.com

## Application Type
Group Scheduler (Doodle Clone)

## Overview
A group scheduling web application similar to Doodle, built with Flask, SQLite, and Tailwind CSS. Users can create polls to find the best date for group events.

## Current State
- Fully functional application with all core features implemented
- Session-based authentication using email
- SQLite database for data persistence

## User Flow
1. **Login**: Email verification + password setup for new users
2. **Dashboard**: Landing page showing all polls (created or participated in)
3. **Create Poll**: Enter poll name, select dates using calendar
4. **Share**: Copy poll URL (5-character short codes)
5. **Vote**: Participants vote Yes/No/Maybe on dates
6. **View Results**: Matrix grid shows all votes, best dates highlighted
7. **Profile**: Edit display name and profile picture

## Project Architecture

### Backend (main.py)
- Flask application with routes for all functionality
- SQLite database with three tables: polls, dates, votes
- Session management for user authentication

### Templates
- `base.html` - Base template with navigation
- `login.html` - Login/registration page
- `set_password.html` - Password setup after email verification
- `dashboard.html` - Main landing page showing user's polls
- `home.html` - Create poll form
- `calendar.html` - Date selection with FullCalendar.js
- `share.html` - Poll sharing page with copy URL
- `vote.html` - Voting matrix grid
- `profile.html` - User profile editing

### Database Schema
- **users**: id, email, password_hash, is_verified, display_name, profile_picture, created_at
- **verification_tokens**: id, email, token, created_at, expires_at
- **polls**: id (5-char short code), name, admin_email, invite_emails, created_at
- **dates**: id, poll_id, date
- **votes**: id, date_id, user_email, status (yes/no/maybe)

## Technical Stack
- Python 3.11 with Flask
- PostgreSQL database (persistent across deployments)
- Tailwind CSS via CDN
- FullCalendar.js for date selection

## Running the Application
```bash
python main.py
```
Server runs on port 5000.
