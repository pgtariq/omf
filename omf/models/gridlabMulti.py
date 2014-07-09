''' Powerflow results for one Gridlab instance. '''

import json, os, sys, tempfile, webbrowser, time, shutil, datetime, subprocess
import multiprocessing
from os.path import join as pJoin
from jinja2 import Template
import __util__ as util
import traceback
import _temp
from _temp import *
# Locational variables so we don't have to rely on OMF being in the system path.
_myDir = os.path.dirname(os.path.abspath(__file__))
_omfDir = os.path.dirname(_myDir)

# OMF imports
sys.path.append(_omfDir)
import feeder
from solvers import gridlabd

# Our HTML template for the interface:
with open(pJoin(_myDir,"gridlabMulti.html"),"r") as tempFile:
	template = Template(tempFile.read())
	_temp.template = template
	
def renderTemplate(modelDir="", absolutePaths=False, datastoreNames={}):
	''' Render the model template to an HTML string.
	By default render a blank one for new input.
	If modelDir is valid, render results post-model-run.
	If absolutePaths, the HTML can be opened without a server. '''
	try:
		allInputData = open(pJoin(modelDir,"allInputData.json")).read()
	except IOError:
		allInputData = None
	try:
		allOutputData = open(pJoin(modelDir,"allOutputData.json")).read()
	except IOError:
		allOutputData = None
	if absolutePaths:
		# Parent of current folder.
		pathPrefix = _omfDir
	else:
		pathPrefix = ""
	
	feederList = []
	feederIDs = []
	try:
		inputDict = json.load(open(pJoin(modelDir, "allInputData.json")))
		for key in inputDict:
			if key.startswith("feederName"):
				feederIDs.append(key) 
				feederList.append(inputDict[key])
	except IOError:
		pass
	return template.render(allInputData=allInputData,
		allOutputData=allOutputData, modelStatus=getStatus(modelDir), pathPrefix=pathPrefix,
		datastoreNames=datastoreNames, feederIDs = feederIDs, feederList = feederList)

def run(modelDir, inputDict):
	''' Run the model in a separate process. web.py calls this to run the model.
	This function will return fast, but results take a while to hit the file system.'''
	# Check whether model exist or not
	if not os.path.isdir(modelDir):
		os.makedirs(modelDir)
		inputDict["created"] = str(datetime.datetime.now())
	# MAYBEFIX: remove this data dump. Check showModel in web.py and renderTemplate()
	with open(pJoin(modelDir, "allInputData.json"),"w") as inputFile:
		json.dump(inputDict, inputFile, indent = 4)
	# If we are re-running, remove output:
	try:
		os.remove(pJoin(modelDir,"allOutputData.json"))
	except:
		pass
	backProc = multiprocessing.Process(target = runForeground, args = (modelDir, inputDict,))
	backProc.start()
	print "SENT TO BACKGROUND", modelDir
	with open(pJoin(modelDir, "PPID.txt"),"w+") as pPidFile:
		pPidFile.write(str(backProc.pid))

def runForeground(modelDir, inputDict):
	''' Run the model in its directory. WARNING: GRIDLAB CAN TAKE HOURS TO COMPLETE. '''
	print "STARTING TO RUN", modelDir
	beginTime = datetime.datetime.now()
	feederList = []

	def preRun(modelDir):
		""" Get prepare of data and clean workspace if re-run"""
		# If re-run remove all the data in the subfolders
		for dirs in os.listdir(modelDir):
			if os.path.isdir(pJoin(modelDir, dirs)):
				print "remove subfolders"
				shutil.rmtree(pJoin(modelDir, dirs))

	preRun(modelDir)
	# Get each feeder, prepare data in separate folders, and run there.
	for key in inputDict:
		if key.startswith("feederName"):
			feederDir, feederName = inputDict[key].split("___")
			feederList.append(feederName)
			try:
				os.remove(pJoin(modelDir, feederName, "allOutputData.json"))
			except Exception, e:
				pass
			if not os.path.isdir(pJoin(modelDir, feederName)):
				os.makedirs(pJoin(modelDir, feederName)) # create subfolders for feeders
			shutil.copy(pJoin(_omfDir, "data", "Feeder", feederDir, feederName + ".json"),
				pJoin(modelDir, feederName, "feeder.json"))
			shutil.copy(pJoin(_omfDir, "data", "Climate", inputDict["climateName"] + ".tmy2"),
				pJoin(modelDir, feederName, "climate.tmy2"))
			try:
				startTime = datetime.datetime.now()
				feederJson = json.load(open(pJoin(modelDir, feederName, "feeder.json")))
				tree = feederJson["tree"]
				# Set up GLM with correct time and recorders:
				feeder.attachRecorders(tree, "Regulator", "object", "regulator")
				feeder.attachRecorders(tree, "Capacitor", "object", "capacitor")
				feeder.attachRecorders(tree, "Inverter", "object", "inverter")
				feeder.attachRecorders(tree, "Windmill", "object", "windturb_dg")
				feeder.attachRecorders(tree, "CollectorVoltage", None, None)
				feeder.attachRecorders(tree, "Climate", "object", "climate")
				feeder.attachRecorders(tree, "OverheadLosses", None, None)
				feeder.attachRecorders(tree, "UndergroundLosses", None, None)
				feeder.attachRecorders(tree, "TriplexLosses", None, None)
				feeder.attachRecorders(tree, "TransformerLosses", None, None)
				feeder.groupSwingKids(tree)
				feeder.adjustTime(tree=tree, simLength=float(inputDict["simLength"]),
					simLengthUnits=inputDict["simLengthUnits"], simStartDate=inputDict["simStartDate"])
				# RUN GRIDLABD IN FILESYSTEM (EXPENSIVE!)
				rawOut = gridlabd.runInFilesystem(tree, attachments=feederJson["attachments"], 
					keepFiles=True, workDir=pJoin(modelDir, feederName))
				cleanOut = {}
				# Std Err and Std Out
				cleanOut['stderr'] = rawOut['stderr']
				cleanOut['stdout'] = rawOut['stdout']
				# Time Stamps
				for key in rawOut:
					if '# timestamp' in rawOut[key]:
						cleanOut['timeStamps'] = rawOut[key]['# timestamp']
						break
					elif '# property.. timestamp' in rawOut[key]:
						cleanOut['timeStamps'] = rawOut[key]['# property.. timestamp']
					else:
						cleanOut['timeStamps'] = []
				# Day/Month Aggregation Setup:
				stamps = cleanOut.get('timeStamps',[])
				level = inputDict.get('simLengthUnits','hours')
				# Climate
				for key in rawOut:
					if key.startswith('Climate_') and key.endswith('.csv'):
						cleanOut['climate'] = {}
						cleanOut['climate']['Rain Fall (in/h)'] = util.hdmAgg(rawOut[key].get('rainfall'), sum, level)
						cleanOut['climate']['Wind Speed (m/s)'] = util.hdmAgg(rawOut[key].get('wind_speed'), util.avg, level)
						cleanOut['climate']['Temperature (F)'] = util.hdmAgg(rawOut[key].get('temperature'), max, level)
						cleanOut['climate']['Snow Depth (in)'] = util.hdmAgg(rawOut[key].get('snowdepth'), max, level)
						cleanOut['climate']['Direct Insolation (W/m^2)'] = util.hdmAgg(rawOut[key].get('solar_direct'), sum, level)
				# Voltage Band
				if 'VoltageJiggle.csv' in rawOut:
					cleanOut['allMeterVoltages'] = {}
					cleanOut['allMeterVoltages']['Min'] = util.hdmAgg([float(i / 2) for i in rawOut['VoltageJiggle.csv']['min(voltage_12.mag)']], min, level)
					cleanOut['allMeterVoltages']['Mean'] = util.hdmAgg([float(i / 2) for i in rawOut['VoltageJiggle.csv']['mean(voltage_12.mag)']], util.avg, level)
					cleanOut['allMeterVoltages']['StdDev'] = util.hdmAgg([float(i / 2) for i in rawOut['VoltageJiggle.csv']['std(voltage_12.mag)']], util.avg, level)
					cleanOut['allMeterVoltages']['Max'] = util.hdmAgg([float(i / 2) for i in rawOut['VoltageJiggle.csv']['max(voltage_12.mag)']], max, level)
				# Power Consumption
				cleanOut['Consumption'] = {}
				# Set default value to be 0, avoiding missing value when computing Loads
				cleanOut['Consumption']['Power'] = [0] * int(inputDict["simLength"])
				cleanOut['Consumption']['Losses'] = [0] * int(inputDict["simLength"])
				cleanOut['Consumption']['DG'] = [0] * int(inputDict["simLength"])
				for key in rawOut:
					if key.startswith('SwingKids_') and key.endswith('.csv'):
						oneSwingPower = util.hdmAgg(util.vecPyth(rawOut[key]['sum(power_in.real)'],rawOut[key]['sum(power_in.imag)']), util.avg, level)
						if 'Power' not in cleanOut['Consumption']:
							cleanOut['Consumption']['Power'] = oneSwingPower
						else:
							cleanOut['Consumption']['Power'] = util.vecSum(oneSwingPower,cleanOut['Consumption']['Power'])
					elif key.startswith('Inverter_') and key.endswith('.csv'): 	
						realA = rawOut[key]['power_A.real']
						realB = rawOut[key]['power_B.real']
						realC = rawOut[key]['power_C.real']
						imagA = rawOut[key]['power_A.imag']
						imagB = rawOut[key]['power_B.imag']
						imagC = rawOut[key]['power_C.imag']
						oneDgPower = util.hdmAgg(util.vecSum(util.vecPyth(realA,imagA),util.vecPyth(realB,imagB),util.vecPyth(realC,imagC)), util.avg, level)
						if 'DG' not in cleanOut['Consumption']:
							cleanOut['Consumption']['DG'] = oneDgPower
						else:
							cleanOut['Consumption']['DG'] = util.vecSum(oneDgPower,cleanOut['Consumption']['DG'])
					elif key.startswith('Windmill_') and key.endswith('.csv'):
						vrA = rawOut[key]['voltage_A.real']
						vrB = rawOut[key]['voltage_B.real']
						vrC = rawOut[key]['voltage_C.real']
						viA = rawOut[key]['voltage_A.imag']
						viB = rawOut[key]['voltage_B.imag']
						viC = rawOut[key]['voltage_C.imag']
						crB = rawOut[key]['current_B.real']
						crA = rawOut[key]['current_A.real']
						crC = rawOut[key]['current_C.real']
						ciA = rawOut[key]['current_A.imag']
						ciB = rawOut[key]['current_B.imag']
						ciC = rawOut[key]['current_C.imag']
						powerA = util.vecProd(util.vecPyth(vrA,viA),util.vecPyth(crA,ciA))
						powerB = util.vecProd(util.vecPyth(vrB,viB),util.vecPyth(crB,ciB))
						powerC = util.vecProd(util.vecPyth(vrC,viC),util.vecPyth(crC,ciC))
						oneDgPower = util.hdmAgg(util.vecSum(powerA,powerB,powerC), util.avg, level)
						if 'DG' not in cleanOut['Consumption']:
							cleanOut['Consumption']['DG'] = oneDgPower
						else:
							cleanOut['Consumption']['DG'] = util.vecSum(oneDgPower,cleanOut['Consumption']['DG'])
					elif key in ['OverheadLosses.csv', 'UndergroundLosses.csv', 'TriplexLosses.csv', 'TransformerLosses.csv']:
						realA = rawOut[key]['sum(power_losses_A.real)']
						imagA = rawOut[key]['sum(power_losses_A.imag)']
						realB = rawOut[key]['sum(power_losses_B.real)']
						imagB = rawOut[key]['sum(power_losses_B.imag)']
						realC = rawOut[key]['sum(power_losses_C.real)']
						imagC = rawOut[key]['sum(power_losses_C.imag)']
						oneLoss = util.hdmAgg(util.vecSum(util.vecPyth(realA,imagA),util.vecPyth(realB,imagB),util.vecPyth(realC,imagC)), util.avg, level)
						if 'Losses' not in cleanOut['Consumption']:
							cleanOut['Consumption']['Losses'] = oneLoss
						else:
							cleanOut['Consumption']['Losses'] = util.vecSum(oneLoss,cleanOut['Consumption']['Losses'])
				# Aggregate up the timestamps:
				if level=='days':
					cleanOut['timeStamps'] = util.aggSeries(stamps, stamps, lambda x:x[0][0:10], 'days')
				elif level=='months':
					cleanOut['timeStamps'] = util.aggSeries(stamps, stamps, lambda x:x[0][0:7], 'months')
				# Write the output.
				with open(pJoin(modelDir, feederName, "allOutputData.json"),"w") as outFile:
					json.dump(cleanOut, outFile, indent=4)
				# Update the runTime in the input file.
				endTime = datetime.datetime.now()
				inputDict["runTime"] = str(datetime.timedelta(seconds=int((endTime - startTime).total_seconds())))
				with open(pJoin(modelDir, feederName, "allInputData.json"),"w") as inFile:
					json.dump(inputDict, inFile, indent=4)
				# Clean up the PID file.
				os.remove(pJoin(modelDir, feederName,"PID.txt"))
				print "DONE RUNNING", modelDir, feederName
			except Exception as e:
				print "Oops, Model Crashed!!!" 
				cancel(pJoin(modelDir, feederName))
				with open(pJoin(modelDir, feederName, "stderr.txt"), "a+") as stderrFile:
					traceback.print_exc(file = stderrFile)

	finishTime = datetime.datetime.now()
	inputDict["runTime"] = str(datetime.timedelta(seconds = int((finishTime - beginTime).total_seconds())))
	with open(pJoin(modelDir, "allInputData.json"),"w") as inFile:
		json.dump(inputDict, inFile, indent = 4)

	def afterRun(modelDir):
		""" Integrate data into allOutputData.json, if error happens, cancel it """
		try:
			output = {}
			output["failures"] = {}
			numOfFeeders = 0
			for root, dirs, files in os.walk(modelDir):
				# dump error info into dict
				if "stderr.txt" in files:
					with open(pJoin(modelDir, root, "stderr.txt"), "r") as stderrFile:
						tempString = stderrFile.read()
						if "ERROR" in tempString or "FATAL" in tempString or "Traceback" in tempString:
							output["failures"]["feeder_" + str(os.path.split(root)[-1])] = {"stderr": tempString}
							continue
				# dump simulated data into dict
				if "allOutputData.json" in files:
					with open(pJoin(modelDir, root, "allOutputData.json"), "r") as feederOutputData:
						numOfFeeders += 1
						feederOutput = json.load(feederOutputData)
						# TODO: a better feeder name
						output["feeder_"+str(os.path.split(root)[-1])] = {}
						output["feeder_"+str(os.path.split(root)[-1])]["Consumption"] = feederOutput["Consumption"]
						output["feeder_"+str(os.path.split(root)[-1])]["allMeterVoltages"] = feederOutput["allMeterVoltages"]
						output["feeder_"+str(os.path.split(root)[-1])]["stderr"] = feederOutput["stderr"]
						output["feeder_"+str(os.path.split(root)[-1])]["stdout"] = feederOutput["stdout"]
						# output[root] = {feederOutput["Consumption"], feederOutput["allMeterVoltages"], feederOutput["stdout"], feederOutput["stderr"]}
			output["numOfFeeders"] = numOfFeeders
			output["timeStamps"] = feederOutput["timeStamps"]
			output["climate"] = feederOutput["climate"]
			with open(pJoin(modelDir,"allOutputData.json"),"w") as outFile:
				json.dump(output, outFile, indent=4)
			try:
				os.remove(pJoin(modelDir, "PPID.txt"))
			except:
				pass
		except Exception, e:
			print "Crashed", e
			try:
				os.remove(pJoin(modelDir, "PPID.txt"))
			except:
				pass
			cancel(modelDir)
		
	afterRun(modelDir)

def _tests():
	# Variables
	workDir = pJoin(_omfDir,"data","Model")
	inData = { 
		"modelName": "Automated Multiple GridlabD Testing",
		# "modelName": "All",
		"simStartDate": "2012-04-01",
		"simLengthUnits": "hours",
		# "feederName": "admin___Simple Market System",
		# "feederName2": "admin___Simple Market System BROKEN", 		# configure error
		# "feederName3": "public___13 Node Embedded DO NOT SAVE",		# feeder error
		# "feederName4": "public___13 Node Ref Feeder Flat",
		# "feederName5": "public___13 Node Ref Feeder Laid Out ZERO CVR",
		# "feederName6": "public___13 Node Ref Feeder Laid Out",
		# "feederName7": "public___ABEC Columbia",
		# "feederName8": "public___ABEC Frank LO Houses",				# feeder error
		# "feederName9": "public___ABEC Frank LO",
		# "feederName10": "public___ACEC Geo",
		# "feederName11": "public___Battery 13 Node Centralized",
		# "feederName12": "public___Battery 13 Node Distributed",
		# "feederName13": "public___DEC Red Base",
		# "feederName14": "public___DEC Red Battery",
		# "feederName15": "public___DEC Red CVR",
		# "feederName16": "public___DEC Red DG",
		# "feederName17": "public___INEC Renoir",
		# "feederName18": "public___Olin Barre CVR Base",
		# "feederName19": "public___Olin Barre Geo",
		# "feederName20": "public___Olin Barre Housed 05Perc Solar",
		# "feederName21": "public___Olin Barre Housed 20Perc Solar",
		# "feederName22": "public___Olin Barre Housed 50Perc Solar",
		# "feederName23": "public___Olin Barre Housed 90Perc Solar",
		# "feederName24": "public___Olin Barre Housed Battery",
		# "feederName25": "public___Olin Barre Housed Wind",
		# "feederName26": "public___Olin Barre Housed",
		# "feederName27": "public___Olin Barre", 						# feeder error
		# "feederName28": "public___PNNL Taxonomy Feeder 1",
		# "feederName29": "public___Simple Market System Comm Solar",
		# "feederName30": "public___Simple Market System Indy Solar",
		"feederName31": "public___Simple Market System",
		# "feederName": "public___Battery 13 Node Distributed",		
		"modelType": "gridlabMulti",
		"climateName": "AL-HUNTSVILLE",
		"simLength": "24",
		"user": "admin", # Really only used with web.py.
		"runTime": ""}
	modelLoc = pJoin(workDir,inData["user"],inData["modelName"])
	# Blow away old test results if necessary.
	try:
		shutil.rmtree(modelLoc)
	except:
		# No previous test results.
		pass
	# No-input template.
	# renderAndShow()
	# Run the model.
	run(modelLoc, inData)
	## Cancel the model.
	# time.sleep(2)
	# cancel(modelLoc)
	# Show the output.
	# renderAndShow(modelDir=modelLoc)
	# Delete the model.
	# shutil.rmtree(modelLoc)

if __name__ == '__main__':
	_tests()