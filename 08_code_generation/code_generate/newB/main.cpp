#include <QCoreApplication>
#include <QDomDocument>
#include <QFile>
#include <QDebug>
#include "messageconvert.h"
 int  readMessageXML(QString path, QVector<std::shared_ptr<messageConvert::NetInfo>> &netlist)    
{        
	// 创建QFile对象，打开XML文件        
	QFile file(path);        
	if (!file.open(QIODevice::ReadOnly | QIODevice::Text))        
	{            
		qDebug() << "Cannot open file for reading: " << qPrintable(file.errorString());            
		return 1;        
	 }        
	 QDomDocument doc;        
	 if (!doc.setContent(&file))        
	 {            
		 qDebug() << "Failed to load document";            
		 file.close();            
		 return 2;        
	 }        
	file.close();        
	QDomElement root = doc.documentElement();        
	QString msgNames = root.attribute("xmlns");        
	if (msgNames.isEmpty())        
	{            
		auto list = root.attributes();            
		int attrLen = list.size();            
		for (int i = 0; i < attrLen; i++)            
		{                
			QString name = list.item(i).nodeName();                
			if (name.indexOf("xmlns") != -1)                
			{                    
				msgNames = list.item(i).nodeValue();                    
				break;                
			}            
		}        
	}        
	qDebug() << "msgName: " << msgNames;        
	//创建消息信息       
	QDomNodeList childNodes = root.childNodes();       
	for (int i = 0; i < childNodes.count(); ++i)       
	{            
		QDomNode node = childNodes.at(i);            
		auto ip = node.attributes().namedItem("ip");            
		auto port = node.attributes().namedItem("port");            
		auto type = node.attributes().namedItem("type");            
		auto recv = node.attributes().namedItem("recv");            
		auto name = node.attributes().namedItem("name");            
		auto feedBackPort = node.attributes().namedItem("feedBackPort");            
		std::shared_ptr<messageConvert::NetInfo> net(new messageConvert::NetInfo);            
		net->ip = ip.nodeValue();            
		net->name = name.nodeValue().remove(".").remove("_");            
		net->port = port.nodeValue().toInt();            
		net->feedBackPort = feedBackPort.nodeValue().toInt();            
		net->bRecvTag = recv.nodeValue().toInt();            
		if (type.nodeValue().toUpper() == "TCP")                
			net->netType = messageConvert::emTCP;            
		else if (type.nodeValue().toUpper() == "UDP")                
			net->netType = messageConvert::emUDP;            
		else if (type.nodeValue().toUpper() == "DDS")                
			net->netType = messageConvert::emDDS;            
		netlist.push_back(net);        
	}        
	return 0;    
}    
int main(int argc, char* argv[])    
{        
	QCoreApplication a(argc, argv);        
	QVector<std::shared_ptr<messageConvert::NetInfo>> netlist;        
	QString exeDirectory = QCoreApplication::applicationDirPath() + "/config.xml";        
	readMessageXML(exeDirectory, netlist);        
	messageConvert obj;        
	obj.start(netlist);        
	return a.exec();    
}    