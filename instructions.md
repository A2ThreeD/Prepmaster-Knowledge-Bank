# Project Name: Prepmaster Knowledge Bank
# Project Description:
 - This is an instructions file for using AI tools to work on creation and modification of a Raspberry Pi based system to store emergency prep and survival information offline.
 - The project will use a standard Lite install of the Raspberry Pi OS
 - The core software that will be used to serve the information is Kiwix and Offline Mapss
 - There should be an installer script that a user can run that will fetch all the parts from various repos to install all of the software needed for this project.
 - For reference, the IIAB repo is https://github.com/iiab/iiab

# User Interface / Web Site Design
- The webpages should have a clean and modern look.
- They should have a good organized sturction for information related to suvival.
- In the event of a major emergency, this system will be used to get information on survival, medical, and general knowledge
- The top box should say "Prepmaster Emergency Knowledge Hub", with a brief overvia of the different core categories.
- Core categories are Quick Access, Critical, Sustainment, Rebuild, and System Tools
- Kiwix Full Library button at the top of the page as the main reference in the Quick Access section path is /kiwix
- Button on the main page to reach the Admin portal of the site located in the relative path /admin
- Button for the offline maps in the relative path /maps
- A vile named index.html.framework which is a backup of the design language that should be used in the project.
- Modify the admin pages to have similar color schemes as the main page for uniformity.

- The following HTML gives the basic framework/style of the site:
    :root {
    --bg: #0b1115;
    --panel: #121c22;
    --panel-2: #1a2830;
    --text: #eef5f6;
    --muted: #a9bdc1;
    --accent: #5fd1b8;
    --accent-2: #f4b860;
    --border: #2d4048;
    }
    * { box-sizing: border-box; }
    body {
    margin: 0;
    color: var(--text);
    background:
        radial-gradient(1200px 500px at 100% -20%, rgba(95, 209, 184, 0.15), transparent 70%),
        radial-gradient(1000px 400px at -10% 110%, rgba(244, 184, 96, 0.12), transparent 60%),
        var(--bg);
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    line-height: 1.45;
    padding-bottom: 86px;
    }

# System Status Section
- A section at bottom of page seperated from the main.
- A small status bar indicating disk capacity (free/used)
- Information such as system temperature and other important statistics for the raspberry pi.
- FUTURE: Battery life indicator where possible

# ZIM File Information
 - ZIM files for the Kiwix server are stored in /library/zims/content
 - A script in the home directory on this image contains a script named download_kiwix_zims.sh that has a curated list of the ZIM files needed
 - The curated list is compiled from the kiwix-categories.json file of Project NOMAD (https://github.com/Crosstalk-Solutions/project-nomad)
 - Use the project nomad kiwix-categories.json to build out the download_kiwix_zims.sh script. That script should be a shell script that uses wget to download a zim file if it's newer or not present to the /library/zims/content folder.  Example line:
    sudo wget -N -c "https://download.kiwix.org/zim/other/zimgit-medicine_en_2024-08.zim"

## Current Categories (Completed)
- [x] Medicine - Medical references, first aid, emergency care
- [x] Survival & Preparedness - Food prep, prepper videos, repair guides
- [x] Education & Reference - Wikipedia, textbooks, TED talks