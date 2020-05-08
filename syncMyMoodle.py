import requests, pickle
from bs4 import BeautifulSoup as bs
import os
import unicodedata
import string
import re
from contextlib import closing
import urllib.parse
import youtube_dl
import json
from config import *
from helper import *

### Main program

session = login(user, password)
response = session.get('https://moodle.rwth-aachen.de/my/', params=params)
soup = bs(response.text, features="html.parser")

## Get Courses
categories = [(c["value"], c.text) for c in soup.find("select", {"name": "coc-category"}).findAll("option")]
categories.remove(('all', 'All'))
selected_categories = [c for c in categories if c == max(categories,key=lambda item:int(item[0])) ] if onlyfetchcurrentsemester else categories
courses = [(c.find("h3").find("a")["href"], semestername) for (sid, semestername) in selected_categories for c in soup.select(f".coc-category-{sid}")]

##DEBUG
#courses=[courses[1]]

for cid, semestername in courses:
	response = session.get(cid, params=params)
	soup = bs(response.text, features="html.parser")

	coursename = clean_filename(soup.select(".page-header-headings")[0].text)
	print(f"Syncing {coursename}...")

	# Get Sections. Some courses have them on one page, others on multiple, then we need to crawl all of them
	sectionpages = re.findall("&section=[0-9]+",response.text)
	if sectionpages:
		sections = []
		for s in sectionpages:
			response = session.get(cid+s, params=params)
			tempsoup = bs(response.text, features="html.parser")
			opendatalti = tempsoup.find("form", {"name": "ltiLaunchForm"})
			sections.extend([(c,opendatalti) for c in tempsoup.select(".topics")[0].children])
		loginOnce = False
	else:
		sections = [(c,soup.find("form", {"name": "ltiLaunchForm"})) for c in soup.select(".topics")[0].children]
		loginOnce = True

	for sec,opendatalti in sections:
		sectionname = clean_filename(sec.attrs["aria-label"])
		mainsectionpath = os.path.join(basedir,semestername,coursename,sectionname)

		# Categories can be multiple levels deep like folders, see https://moodle.rwth-aachen.de/course/view.php?id=7053&section=1

		label_categories = sec.findAll("li", {"class": [
			"modtype_label", 
			"modtype_resource", 
			"modtype_url", 
			"modtype_folder", 
			"modtype_assign", 
			"modtype_page"]})

		categories = []
		category = None
		for l in label_categories:
			if "modtype_label" in l['class'] and enableExperimentalCategories:
				category = (clean_filename(l.findAll(text=True)[-1]), [])
				categories.append(category)
			else:
				if category == None:
					category = (None, [])
					categories.append(category)
				category[1].append(l)
		## Get Opencast Videos directly embedded in section
		if opendatalti:
			downloadOpenCastVideos(sec, opendatalti, mainsectionpath, session, loginOnce)
			loginOnce = False

		for c in categories:
			if c[0] == None:
				sectionpath = mainsectionpath
			else:
				sectionpath = os.path.join(mainsectionpath, c[0])
			for s in c[1]:
				## Get Resources
				if "modtype_resource" in s["class"] and s.find('a', href=True):
					r = s.find('a', href=True)["href"]
					# First check if the file is a video:
					if not download_file(r,sectionpath, session): # No filenames here unfortunately
					# If no file was found, then its probably an html page with an enbedded video
						response = session.get(r, params=params)
						if "Content-Type" in response.headers and "text/html" in response.headers["Content-Type"]:
							tempsoup = bs(response.text, features="html.parser")
							videojs = tempsoup.select(".video-js")
							if videojs:
								videojs = tempsoup.select(".video-js")[0].find("source")
								if videojs and "src" in videojs.attrs.keys():
									download_file(videojs["src"],sectionpath, session, videojs["src"].split("/")[-1])

				## Get Resources in URLs
				if "modtype_url" in s["class"] and s.find('a', href=True):
					r = s.find('a', href=True)["href"]
					response = session.head(r, params=params)
					if "Location" in response.headers:
						url = response.headers["Location"]
						response = session.head(url, params=params)
						if "Content-Type" in response.headers and "text/html" not in response.headers["Content-Type"]: # Don't download html pages
							download_file(url,sectionpath, session)

				## Get Folders
				if "modtype_folder" in s["class"] and s.find('a', href=True):
					f = s.find('a', href=True)["href"]
					response = session.get(f, params=params)
					soup = bs(response.text, features="html.parser")
					soup_results = soup.find("a",{"title": "Folder"})

					if soup_results is not None:
						foldername = clean_filename(soup_results.text)

						filemanager = soup.select(".filemanager")[0].findAll('a', href=True)
						# Scheiß auf folder, das mach ich 1 andernmal
						for file in filemanager:
							link = file["href"]
							filename = file.select(".fp-filename")[0].text
							download_file(link, os.path.join(sectionpath,foldername), session, filename)

				## Get Assignments
				if "modtype_assign" in s["class"] and s.find('a', href=True):
					a = s.find('a', href=True)["href"]
					response = session.get(a, params=params)
					soup = bs(response.text, features="html.parser")
					files = soup.select(".fileuploadsubmission")
					soup_results = soup.find("a",{"title": "Assignment"})

					if soup_results is not None:
						foldername = clean_filename(soup_results.text)
						
						for file in files:
							link = file.find('a', href=True)["href"]
							filename = file.text
							if len(filename) > 2 and filename[0] == " " and filename[-1] == " ": # Remove space around the file
								filename = filename[1:-1]
							download_file(link, os.path.join(sectionpath,foldername), session, filename)

				## Get embedded videos in pages
				if "modtype_page" in s["class"] and s.find('a', href=True):
					p = s.find('a', href=True)["href"]
					response = session.get(p, params=params)
					soup = bs(response.text, features="html.parser")
					soup_results = soup.find("a",{"title": "Page"})

					if soup_results is not None:
						pagename = clean_filename(soup_results.text)

						links = re.findall("https://www.youtube.com/embed/.{11}", response.text)
						path = os.path.join(sectionpath,pagename)
						if not os.path.exists(path):
							os.makedirs(path)
						finallinks = []
						for l in links:
							if len([f for f in os.listdir(path) if l[-11:] in f])==0:
								finallinks.append(l)
						ydl_opts = {
										"outtmpl": "{}/%(title)s-%(id)s.%(ext)s".format(path),
										"ignoreerrors": True,
										"nooverwrites": True,
										"retries": 15
									}
						with youtube_dl.YoutubeDL(ydl_opts) as ydl:
							ydl.download(finallinks)

						opendataltipage = soup.find("form", {"name": "ltiLaunchForm"})
						if opendataltipage: # Opencast in pages embedded
							downloadOpenCastVideos(soup, opendataltipage, path, session, False)