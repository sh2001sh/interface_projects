#include "codec.h"
#include <iostream>                 
#include <QByteArray>                 
#include <QDataStream>                 
#include <algorithm>                 
#include <string>                 
#include <QDebug>                
#include <QMap>                
#include <functional>              
static int BigAndLittleFlag = 1;
#define BigtoLittle32(A) ((((uint32_t)(A) & 0xff000000) >> 24) | (((uint32_t)(A) & 0x00ff0000) >> 8) | (((uint32_t)(A) & 0x0000ff00) << 8) | (((uint32_t)(A) & 0x000000ff) << 24))
#define LittleToBig32(A) ((((uint32_t)(A) & 0xff000000) >> 24) | (((uint32_t)(A) & 0x00ff0000) >> 8) | (((uint32_t)(A) & 0x0000ff00) << 8) | (((uint32_t)(A) & 0x000000ff) << 24))             
class MessCodeInfo{                
public:                 
	int   icycle;                        
	int   itimes;                         
	QString strSeq;                    
};         
static quint64 readBits(const uchar * data, int dataLength, int startBitIndex, int numBits)
{
        
	if (numBits <= 0 || numBits > 64 || startBitIndex < 0 || startBitIndex + numBits >(dataLength * 8)) {            
		qWarning() << "Invalid parameters";            
		return 0;        
	}        
	quint32 result = 0;        
	// 计算起始字节和起始位在该字节的位置        
	int startByteIndex = startBitIndex / 8;        
	int startBitInStartByte = startBitIndex % 8;        
	// 遍历所有影响结果的字节        
	for (int i = startByteIndex; numBits > 0 && i < dataLength; ++i) {           
 		quint8 currentByte = data[i];           
 		// 计算本次需要读取的位数（不超过8位）           
 		int bitsToRead = std::min(numBits, 8 - startBitInStartByte);           
 		// 移动当前字节中的目标位到最低位，然后按位与以保留目标位           
 		quint8 shiftedByte = (currentByte >> (8 - startBitInStartByte - bitsToRead)) & ((1u << bitsToRead) - 1);            
		// 左移结果并将新读取的位添加进去            
		result = (result << bitsToRead) | shiftedByte;           
 		// 更新剩余位数和下次循环的起始位位置           
 		numBits -= bitsToRead;            
		startBitInStartByte = 0;        
	}        
	return result;    
}
        
static void checkPosition(uchar * &pData,int & msgLens,int & pos,int num )
{    
	if((pos + num ) > 32)
	{        
		pData += pos / 8;        
		msgLens -= pos / 8;        
		pos = pos % 8;    
	}
}
static quint64 readBitsLE(const uchar * data, int dataLength, int startBitIndex, int numBits)
{
    if (numBits <= 0 || numBits > 64 || startBitIndex < 0 || startBitIndex + numBits >(dataLength * 8)) {
        qWarning() << "Invalid parameters";
        return 0;
    }
    quint64 result = 0;
    char* p = (char*)&BigAndLittleFlag;
    if (*p == 1)//小端
    {
        int startByteIndex = startBitIndex / 8;                                                      
        int startBitInStartByte = startBitIndex % 8;                                                 
        int readBits = numBits % 8;                                                                  
        if (startBitInStartByte == 0 && readBits == 0)//整字节                                       
        {                                                                                            
            memcpy(&result, data + startByteIndex, numBits / 8);                                     
        }                                                                                            
        else//按为读取                                                                               
        {                                                                                            
            // 遍历所有影响结果的字节                                                                
            for (int i = startByteIndex; numBits > 0 && i < dataLength; ++i) {                       
                quint8 currentByte = data[i];                                                        
                // 计算本次需要读取的位数（不超过8位）                                               
                int bitsToRead = std::min(numBits, 8 - startBitInStartByte);                         
                // 移动当前字节中的目标位到最低位，然后按位与以保留目标位                            
                quint8 shiftedByte = (currentByte >> startBitInStartByte) & ((1u << bitsToRead) - 1);
                // 左移结果并将新读取的位添加进去                                                    
                result |= shiftedByte << ((i - startByteIndex) * bitsToRead);                        
                // 更新剩余位数和下次循环的起始位位置                                                
                numBits -= bitsToRead;                                                               
                startBitInStartByte = 0;                                                             
            }                                                                                        
        }                                                                                            
    }                                                                                                
    else//大端                                                                                       
    {                                                                                                
        // 计算起始字节和起始位在该字节的位置                                                        
        int startByteIndex = startBitIndex / 8;                                                      
        int startBitInStartByte = startBitIndex % 8;                                                 
        // 遍历所有影响结果的字节                                                                    
        for (int i = startByteIndex; numBits > 0 && i < dataLength; ++i) {                           
            quint8 currentByte = data[i];                                                            
            // 计算本次需要读取的位数（不超过8位）                                                   
            int bitsToRead = std::min(numBits, 8 - startBitInStartByte);                             
            // 移动当前字节中的目标位到最低位，然后按位与以保留目标位                                
            quint8 shiftedByte = (currentByte >> startBitInStartByte) & ((1u << bitsToRead) - 1);    
            // 左移结果并将新读取的位添加进去                                                        
            result |= shiftedByte << (numBits - bitsToRead);                                         
            // 更新剩余位数和下次循环的起始位位置                                                    
            numBits -= bitsToRead;                                                                   
            startBitInStartByte = 0;                                                                 
        }                                                                                            
    }                                                                                                
    return result;                                                                                   
}                                                                                                    
static void appendBits(uint64_t value, size_t bitSize, QByteArray & byteArray, bool bigEndian ,bool bclear = false)       
{                                                                                                    
    static uint64_t cachedBits = 0; // 缓存的位数据                                                  
    static size_t cachedBitCount = 0; // 已缓存的位数                                                
    char* p = (char*)&BigAndLittleFlag;                                                              
	if(bclear)
	{                    
		cachedBits = 0;                    
		cachedBitCount = 0;                
	}
    if (*p == 1)//小端                                                                               
    {                                                                                                
        if (bigEndian)                                                                               
        {                                                                                            
            if (bitSize > 8)                                                                         
            {                                                                                        
              value &=(1<<bitSize) -1;                                                       
            }                                                                                        
            // 将新数据加入缓存                                                                      
            cachedBits = (cachedBits << bitSize) | (value & ((1 << bitSize) - 1));                   
            cachedBitCount += bitSize;                                                               
                                                                                                     
        }                                                                                            
        else                                                                                         
        {                                                                                            
            // 将新数据加入缓存                                                                      
            //cachedBits = (cachedBits << bitSize) | (value & ((1 << (bitSize)) - 1));               
            if (cachedBitCount == 0 && (bitSize % 8 == 0))
           {
               cachedBits = value;
            }
            else
            cachedBits = (cachedBits) | (value & ((1 << (bitSize + cachedBitCount)) - 1));           
            cachedBitCount += bitSize;                                                               
        }                                                                                            
        while (cachedBitCount >= 8)                                                                  
        {                                                                                            
            // 计算当前字节的位移                                                                    
            size_t shift = bigEndian ? cachedBitCount - 8 : 0;                                     
            // 获取当前字节                                                                          
            char byte = static_cast<char>((cachedBits >> shift) & 0xFF);                           
            //char byte = static_cast<char>(cachedBits & 0xFF);                                        
            // 添加到字节数组                                                                        
            byteArray.append(byte);                                                                  
            // 更新缓存中的位数                                                                      
            cachedBitCount -= 8;                                                                     
            //// 如果是大端，左移；如果是小端，右移                                                  
            if (bigEndian)                                                                         
              cachedBits &= (1<< cachedBitCount) -1;                                                                        
            else                                                                                   
              cachedBits >>= 8;
        }                                                                                            
    }                                                                                                
    else//大端                                                                                       
    {                                                                                                
    }                                                                                                
}     
static double readBits_d(const uchar* data, int dataLength, int startBitIndex, int numBits)    
{        
	if (numBits <= 0 || numBits > 64 || startBitIndex < 0 || startBitIndex + numBits >(dataLength * 8)) {            
		qWarning() << "Invalid parameters";            
		return 0.0;        
	}        
	double result = 0.0;        
	int startByteIndex = startBitIndex / 8;        
	int startBitInStartByte = startBitIndex % 8;        
	int bitsRemaining = numBits;        
	for (int i = startByteIndex; bitsRemaining > 0 && i < dataLength; ++i) {            
		quint8 currentByte = data[i];            
		int bitsToRead = std::min(bitsRemaining, 8 - startBitInStartByte);            
		quint8 shiftedByte = (currentByte >> (8 - startBitInStartByte - bitsToRead)) & ((1u << bitsToRead) - 1);            
		result += static_cast<double>(shiftedByte) * pow(2, -bitsRemaining);            
		bitsRemaining -= bitsToRead;            
		startBitInStartByte = 0;        
	}        
	return result;   
	 }    
static double readBitsLE_d(const uchar * data, int dataLength, int startBitIndex, int numBits)    
{        
	if (numBits <= 0 || numBits > 64 || startBitIndex < 0 || startBitIndex + numBits >(dataLength * 8)) {            
		qWarning() << "Invalid parameters";            
		return 0.0;        
	}        
	double result = 0.0;        
	int startByteIndex = startBitIndex / 8;        
	int startBitInStartByte = startBitIndex % 8;        
	int bitsRemaining = numBits;        
	for (int i = startByteIndex; bitsRemaining > 0 && i < dataLength; ++i) {            
		quint8 currentByte = data[i];            
		int bitsToRead = std::min(bitsRemaining, 8 - startBitInStartByte);            
		quint8 shiftedByte = (currentByte >> startBitInStartByte) & ((1u << bitsToRead) - 1);            
		result += static_cast<double>(shiftedByte) * pow(2, -(startBitIndex + numBits - (i * 8 + startBitInStartByte)));            
		bitsRemaining -= bitsToRead;            
		startBitInStartByte = 0;        
	}        
	return result;    
}    
static void appendBits_d(double value, size_t bitSize, QByteArray& byteArray, bool bigEndian)    
{                
	static qulonglong cachedBits = 0; // Cached bit data                
	static size_t cachedBitCount = 0; // Number of bits already cached                
	// Convert double to 64-bit integer                
	uint64_t intValue = *reinterpret_cast<qulonglong*>(&value);                
	// Mask value to only keep the desired number of bits                
	intValue &= ((1ULL << bitSize) - 1);                
	// Add new data to the cache                
	cachedBits = (cachedBits << bitSize) | intValue;                
	cachedBitCount += bitSize;                
	while (cachedBitCount >= 8)                
	{                    
		// Calculate the bit shift for the current byte                    
		size_t shift = bigEndian ? cachedBitCount - 8 : 0;                    
		// Get the current byte                    
		char byte = static_cast<char>((cachedBits >> shift) & 0xFF);                    
		// Append the byte to the byte array                    
		byteArray.append(byte);                    
		// Update the number of cached bits                    
		cachedBitCount -= 8;                    
		// Adjust the cached bits based on endianness                    
		if (bigEndian)                    
		    cachedBits &= ((1ULL << shift) - 1);                    
		else                    
		    cachedBits >>= 8;                
	}            
	}    

void readOrigin(ICD304&iCD304,uchar * & pMsgData, int & msgLens,int & pos){
// body
	checkPosition(pMsgData, msgLens,pos,16);
	iCD304.var16216120 = readBits(pMsgData, msgLens, pos ,16);
	pos += 16;

	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var16216191 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var16216242 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var16216293 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var16216354 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var16216415 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,64);
	iCD304.var16216466 = readBits_d(pMsgData, msgLens, pos ,64);
	pos += 64;

}
QString  checkICD304SeqNum(QString strSeq)
{
	
	static QMap<QString,std::shared_ptr<MessCodeInfo>>  proSeqMap; 
	static int index = 0;                     
	if( 0 == index++ )
	{
     
	std::shared_ptr<MessCodeInfo> Seq_1(new MessCodeInfo); 
	Seq_1->icycle=0;
	Seq_1->itimes=1;
	Seq_1->strSeq="3";
	proSeqMap["Seq_1"] = Seq_1; 

	}
	QString ret;
	auto ptr = proSeqMap.begin();        
	while (ptr != proSeqMap.end())        
	{            
		if (ptr.value()->strSeq == strSeq)            
		{                
			ret = ptr.key();                
			break;            
		}            
		ptr++;        
	}

	return ret;
        
}
void readOrigin(ICD304&iCD304,uchar * & pMsgData, int & msgLens,int & pos);
void readICD304(int key,ICD304 &iCD304,uchar * & pMsgData, int & msgLens,int & pos)
{
	switch (key)
{
	case 3:readOrigin(iCD304,pMsgData,msgLens,pos);break; 
	default:
		break;
	}
}

void writeOrigin(ICD304&iCD304,QByteArray& data);
void updateFieldValue(ICD304 &iCD304);
void VerifyField(ICD304 &iCD304);
void updateGroupFlag(ICD304 &iCD304);
static void writeSeq_1(ICD304&iCD304,QByteArray& data)
{

	VerifyField(iCD304);
	updateFieldValue(iCD304);
	updateGroupFlag(iCD304);
	writeOrigin(iCD304,data);
}

int checkObjMaps(QString strVerify,QByteArray& data, ICD304 &iCD304)
{
	return 0;
}static QString  VerifyiCD304Seq(ICD304&iCD304,QString seq)
{
	return " iCD304";
}
QString decodeMsg( uchar * pData, int len, ICD304 &iCD304){
	 int pos = 0;
	 unsigned char *pMsgData = pData;
	 int msgLens = len;
	 int index = 1;

// head
	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var16216497 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var16216518 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var16216519 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var162165110 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,8);
	iCD304.var162165111 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	readOrigin(iCD304, pMsgData, msgLens, pos); 

	int ip= pos%8 == 0 ? 0:(8 - pos%8);
	pos+=ip;
		checkPosition(pMsgData, msgLens, pos, ip); 
	QStringList seqNum;
	seqNum.append("3");
	if(3!=iCD304.var16216518  ){
	 qDebug()<< __func__ <<" "<<__LINE__<<" recvCode:"<<iCD304.var16216518<<"destCode:" <<"3";
	 return "";
	}
	while( msgLens) 
	{                
		int temPos = pos;                
		unsigned char* pMsg = pMsgData;                
		int temLen = msgLens;                
		int by = 0;                
		int wordFlag = 0;                
		int wontinueWord = 0;
		by=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		checkPosition(pMsg, temLen, temPos, 2); 
		wordFlag=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		if(1==wordFlag)
		{ 
			 int key = wordFlag*10+index++; 
			 if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readICD304(key,iCD304, pMsgData, msgLens, pos); 
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break;
		 }
		else if(2==wordFlag)
		{ 
			wontinueWord=readBits(pMsg, temLen, temPos ,5);
 
			 int key = wordFlag*10+wontinueWord;
			  if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readICD304(key,iCD304, pMsgData, msgLens, pos);
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break; 
		 }
		else{
			 qDebug()<< __func__ <<" "<<__LINE__<<" msgLen:"<<msgLens <<"pos:"<<pos; break; }
		temPos = (pos%8 != 0)?pos/8 +1:pos/8;
		if((msgLens-temPos) == 0){break;}
	}
	QString strSeq,strSeqNum=seqNum.join("");                         
	 strSeq =checkICD304SeqNum( strSeqNum);
	 if(strSeq.isEmpty()==true) return "";
	 QString verifySeq = VerifyiCD304Seq(iCD304, strSeq);
	 qDebug()<<"recv SeqNum:"<<  strSeqNum <<" recv Seq: " << strSeq << " recv verifySeq: " << verifySeq;  
	 if(verifySeq.isEmpty()==true)
	{
		return "";	}
	 return verifySeq;
}


void writeOrigin(ICD304&iCD304,QByteArray& data){
// head
	appendBits(iCD304.var16216497,8,data,true,true);
	appendBits(iCD304.var16216518,8,data,true);
	appendBits(iCD304.var16216519,8,data,true);
	appendBits(iCD304.var162165110,8,data,true);
	appendBits(iCD304.var162165111,8,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(iCD304.var16216120,16,data,true);
	appendBits(iCD304.var16216191,8,data,true);
	appendBits(iCD304.var16216242,8,data,true);
	appendBits(iCD304.var16216293,8,data,true);
	appendBits(iCD304.var16216354,8,data,true);
	appendBits(iCD304.var16216415,8,data,true);
	appendBits_d(iCD304.var16216466,64,data,true);

}
QString checkEncodeSeqNumber(ICD304 &iCD304)
{
	QString seqNum;
	int flag =0;
 
	int index =0; 
	 int count = 0;
	flag = 0;
	flag+=iCD304.var16216120!=0;
	flag+=iCD304.var16216191!=0;
	flag+=iCD304.var16216242!=0;
	flag+=iCD304.var16216293!=0;
	flag+=iCD304.var16216354!=0;
	flag+=iCD304.var16216415!=0;
	flag+=iCD304.var16216466!=0;
	if(flag != 0){seqNum+="3";}
	 return seqNum;}

void VerifyField(ICD304 &iCD304)
{
	int flag = 0; 
}
void updateFieldValue(ICD304 &iCD304)
{
}

void updateGroupFlag(ICD304 &iCD304)
{
}
void encodeMsg(QByteArray& data, ICD304 &iCD304){

	QString strSeqNum=checkEncodeSeqNumber(iCD304);
	QString temSeqNum;
	 temSeqNum="3";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_1
		writeSeq_1(iCD304,data); 
		return;
	}
	writeSeq_1(iCD304,data);
}


void readOrigin(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// body
	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446767732 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,15);
	w304.var17446773733 = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,8);
	w304.var17446774734 = readBits(pMsgData, msgLens, pos ,8);
	pos += 8;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446785735 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17446798736 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,6);
	w304.var17446807737 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,6);
	w304.var17446815738 = readBits(pMsgData, msgLens, pos ,6);
	pos += 6;

	checkPosition(pMsgData, msgLens,pos,14);
	w304.var17446818739 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

}

void readProlong(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744769771 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744779772 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

// body
	checkPosition(pMsgData, msgLens,pos,1);
	w304.var17446832740 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,16);
	w304.var17446840741 = readBits(pMsgData, msgLens, pos ,16);
	pos += 16;

	checkPosition(pMsgData, msgLens,pos,26);
	w304.var17446848742 = readBits(pMsgData, msgLens, pos ,26);
	pos += 26;

	checkPosition(pMsgData, msgLens,pos,25);
	w304.var17446848743 = readBits(pMsgData, msgLens, pos ,25);
	pos += 25;

}

void readContinue1(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744784773 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744796776 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17447117779 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w304.var17446920751 = w304.var1744784773;
	checkPosition(pMsgData, msgLens,pos,14);
	w304.var17446864744 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

	checkPosition(pMsgData, msgLens,pos,14);
	w304.var17446864745 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

	checkPosition(pMsgData, msgLens,pos,14);
	w304.var17446880746 = readBits(pMsgData, msgLens, pos ,14);
	pos += 14;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17446880747 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17446897748 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,4);
	w304.var17446903749 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446912750 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,4);
	w304.var17446920751 = readBits(pMsgData, msgLens, pos ,4);
	pos += 4;

}

void readContinue2(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744784774 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744796777 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17447117780 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
	checkPosition(pMsgData, msgLens,pos,18);
	w304.var17446932752 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,18);
	w304.var17446940753 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,18);
	w304.var17446944754 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446952755 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446960756 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var17446972757 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

}

void readContinue4(W304&w304,uchar * & pMsgData, int & msgLens,int & pos){
// head

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744784775 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744796778 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var17447117781 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

// body
//位数为0的字段，用头中的**字段值代替
	w304.var1744743766 = w304.var1744784775;
	checkPosition(pMsgData, msgLens,pos,16);
	w304.var17446978758 = readBits(pMsgData, msgLens, pos ,16);
	pos += 16;

	checkPosition(pMsgData, msgLens,pos,10);
	w304.var17446988759 = readBits(pMsgData, msgLens, pos ,10);
	pos += 10;

	checkPosition(pMsgData, msgLens,pos,1);
	w304.var17446993760 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,1);
	w304.var174472761 = readBits(pMsgData, msgLens, pos ,1);
	pos += 1;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744710762 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,15);
	w304.var1744718763 = readBits(pMsgData, msgLens, pos ,15);
	pos += 15;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744727764 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744734765 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,18);
	w304.var1744743766 = readBits(pMsgData, msgLens, pos ,18);
	pos += 18;

}
QString  checkW304SeqNum(QString strSeq)
{
	
	static QMap<QString,std::shared_ptr<MessCodeInfo>>  proSeqMap; 
	static int index = 0;                     
	if( 0 == index++ )
	{
     
	std::shared_ptr<MessCodeInfo> Seq_1(new MessCodeInfo); 
	Seq_1->icycle=0;
	Seq_1->itimes=1;
	Seq_1->strSeq="311212224";
	proSeqMap["Seq_1"] = Seq_1; 

	std::shared_ptr<MessCodeInfo> Seq_2(new MessCodeInfo); 
	Seq_2->icycle=2000;
	Seq_2->itimes=3;
	Seq_2->strSeq="3112122";
	proSeqMap["Seq_2"] = Seq_2; 

	std::shared_ptr<MessCodeInfo> Seq_3(new MessCodeInfo); 
	Seq_3->icycle=2000;
	Seq_3->itimes=3;
	Seq_3->strSeq="3112124";
	proSeqMap["Seq_3"] = Seq_3; 

	}
	QString ret;
	auto ptr = proSeqMap.begin();        
	while (ptr != proSeqMap.end())        
	{            
		if (ptr.value()->strSeq == strSeq)            
		{                
			ret = ptr.key();                
			break;            
		}            
		ptr++;        
	}

	return ret;
        
}
void readOrigin(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readProlong(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue1(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue2(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readContinue4(W304&w304,uchar * & pMsgData, int & msgLens,int & pos);
void readW304(int key,W304 &w304,uchar * & pMsgData, int & msgLens,int & pos)
{
	switch (key)
{
	case 3:readOrigin(w304,pMsgData,msgLens,pos);break; 
	case 11:readProlong(w304,pMsgData,msgLens,pos);break;
	case 21:readContinue1(w304,pMsgData,msgLens,pos);break;
	case 22:readContinue2(w304,pMsgData,msgLens,pos);break;
	case 24:readContinue4(w304,pMsgData,msgLens,pos);break;
	default:
		break;
	}
}

void writeContinue1(W304&w304,QByteArray& data);

void writeProlong(W304&w304,QByteArray& data);
void updateGroupFlag(W304 &w304);
void VerifyField(W304 &w304);

void writeContinue2(W304&w304,QByteArray& data);

void writeContinue4(W304&w304,QByteArray& data);
void updateFieldValue(W304 &w304);

void writeOrigin(W304&w304,QByteArray& data);
static void writeSeq_1(W304&w304,QByteArray& data)
{

	VerifyField(w304);
	updateFieldValue(w304);
	updateGroupFlag(w304);
	writeOrigin(w304,data);
	writeProlong(w304,data);
	writeContinue1(w304,data);
	writeContinue2(w304,data);
	writeContinue4(w304,data);
}
static void writeSeq_2(W304&w304,QByteArray& data)
{

	VerifyField(w304);
	updateFieldValue(w304);
	updateGroupFlag(w304);
	writeOrigin(w304,data);
	writeProlong(w304,data);
	writeContinue1(w304,data);
	writeContinue2(w304,data);
}
static void writeSeq_3(W304&w304,QByteArray& data)
{

	VerifyField(w304);
	updateFieldValue(w304);
	updateGroupFlag(w304);
	writeOrigin(w304,data);
	writeProlong(w304,data);
	writeContinue1(w304,data);
	writeContinue4(w304,data);
}

static bool checkAtom_1(W304 &w304)
{// 校验W304_Origin:Item.消息长度值
	return w304.var17446767732==3;

}

static bool setAtom_1(W304 &w304)
{// 设置W304_Origin:Item.消息长度值
	return w304.var17446767732=3;

}

static bool checkAtom_2(W304 &w304)
{// 校验W304_Origin:Item.消息长度值
	return w304.var17446767732==4;

}

static bool setAtom_2(W304 &w304)
{// 设置W304_Origin:Item.消息长度值
	return w304.var17446767732=4;

}

static bool checkConstraint_1(W304 &w304)
{// 计算 Constraint_1值
	return checkAtom_1(w304);

}

static bool setConstraint_1(W304 &w304)
{// 设置 Constraint_1值
	return setAtom_1(w304);

}

static bool checkConstraint_2(W304 &w304)
{// 计算 Constraint_2值
	return checkAtom_2(w304);

}

static bool setConstraint_2(W304 &w304)
{// 设置 Constraint_2值
	return setAtom_2(w304);

}

static QString checkVerify_1(W304 &w304,QString seq)
{
	return  (seq=="Seq_1"&&checkConstraint_2(w304) )?"Verify_1":"" ;

}

static bool setVerify_1(W304 &w304,QByteArray& data)
{
	  setConstraint_2(w304)  ;

	writeSeq_1(w304,data);
	return true;
}

static QString checkVerify_2(W304 &w304,QString seq)
{
	return  (seq=="Seq_2"&&checkConstraint_1(w304) )?"Verify_2":"" ;

}

static bool setVerify_2(W304 &w304,QByteArray& data)
{
	  setConstraint_1(w304)  ;

	writeSeq_2(w304,data);
	return true;
}

static QString checkVerify_3(W304 &w304,QString seq)
{
	return  (seq=="Seq_3"&&checkConstraint_1(w304) )?"Verify_3":"" ;

}

static bool setVerify_3(W304 &w304,QByteArray& data)
{
	  setConstraint_1(w304)  ;

	writeSeq_3(w304,data);
	return true;
}
static QString  Verifyw304Seq(W304&w304,QString seq)
{
	 for(int i=0;i < 3;i++)
	{
		switch (i)
		{
		case 0:                   
			{                   
				QString str = checkVerify_1(w304,seq);                   
				if(  str.isEmpty()==false)                    
					return str;
			}break;
		case 1:                   
			{                   
				QString str = checkVerify_2(w304,seq);                   
				if(  str.isEmpty()==false)                    
					return str;
			}break;
		case 2:                   
			{                   
				QString str = checkVerify_3(w304,seq);                   
				if(  str.isEmpty()==false)                    
					return str;
			}break;
		}
	}
	return "";
}
QString decodeMsg( uchar * pData, int len, W304 &w304){
	 int pos = 0;
	 unsigned char *pMsgData = pData;
	 int msgLens = len;
	 int index = 1;

// head
	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744751767 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,2);
	w304.var1744752768 = readBits(pMsgData, msgLens, pos ,2);
	pos += 2;

	checkPosition(pMsgData, msgLens,pos,5);
	w304.var1744759769 = readBits(pMsgData, msgLens, pos ,5);
	pos += 5;

	checkPosition(pMsgData, msgLens,pos,3);
	w304.var1744768770 = readBits(pMsgData, msgLens, pos ,3);
	pos += 3;

	readOrigin(w304, pMsgData, msgLens, pos); 

	int ip= pos%8 == 0 ? 0:(8 - pos%8);
	pos+=ip;
		checkPosition(pMsgData, msgLens, pos, ip); 
	QStringList seqNum;
	seqNum.append("3");
	while( msgLens) 
	{                
		int temPos = pos;                
		unsigned char* pMsg = pMsgData;                
		int temLen = msgLens;                
		int by = 0;                
		int wordFlag = 0;                
		int wontinueWord = 0;
		by=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		checkPosition(pMsg, temLen, temPos, 2); 
		wordFlag=readBits(pMsg, temLen, temPos ,2);

		temPos+=2;

		if(1==wordFlag)
		{ 
			 int key = wordFlag*10+index++; 
			 if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readW304(key,w304, pMsgData, msgLens, pos); 
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break;
		 }
		else if(2==wordFlag)
		{ 
			wontinueWord=readBits(pMsg, temLen, temPos ,5);
 
			 int key = wordFlag*10+wontinueWord;
			  if (seqNum.indexOf(QString::number(key)) == -1)
				seqNum.append(QString::number(key));
			readW304(key,w304, pMsgData, msgLens, pos);
			pos += pos%8 == 0 ? 0:(8 - pos%8);
			if(temLen==msgLens) break; 
		 }
		else{
			 qDebug()<< __func__ <<" "<<__LINE__<<" msgLen:"<<msgLens <<"pos:"<<pos; break; }
		temPos = (pos%8 != 0)?pos/8 +1:pos/8;
		if((msgLens-temPos) == 0){break;}
	}
	QString strSeq,strSeqNum=seqNum.join("");                         
	 strSeq =checkW304SeqNum( strSeqNum);
	 if(strSeq.isEmpty()==true) return "";
	 QString verifySeq = Verifyw304Seq(w304, strSeq);
	 qDebug()<<"recv SeqNum:"<<  strSeqNum <<" recv Seq: " << strSeq << " recv verifySeq: " << verifySeq;  
	 if(verifySeq.isEmpty()==true)
	{
		return "";	}
	 return verifySeq;
}


void writeOrigin(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744751767,2,data,true,true);
	appendBits(w304.var1744752768,2,data,true);
	appendBits(w304.var1744759769,5,data,true);
	appendBits(w304.var1744768770,3,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var17446767732,3,data,true);
	appendBits(w304.var17446773733,15,data,true);
	appendBits(w304.var17446774734,8,data,true);
	appendBits(w304.var17446785735,3,data,true);
	appendBits(w304.var17446798736,5,data,true);
	appendBits(w304.var17446807737,6,data,true);
	appendBits(w304.var17446815738,6,data,true);
	appendBits(w304.var17446818739,14,data,true);

}

void writeProlong(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744769771,2,data,true);
	appendBits(w304.var1744779772,2,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var17446832740,1,data,true);
	appendBits(w304.var17446840741,16,data,true);
	appendBits(w304.var17446848742,26,data,true);
	appendBits(w304.var17446848743,25,data,true);

}

void writeContinue1(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744784773,2,data,true);
	appendBits(w304.var1744796776,2,data,true);
	appendBits(w304.var17447117779,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var17446920751,0,data,true);
	appendBits(w304.var17446864744,14,data,true);
	appendBits(w304.var17446864745,14,data,true);
	appendBits(w304.var17446880746,14,data,true);
	appendBits(w304.var17446880747,5,data,true);
	appendBits(w304.var17446897748,5,data,true);
	appendBits(w304.var17446903749,4,data,true);
	appendBits(w304.var17446912750,3,data,true);
	appendBits(w304.var17446920751,4,data,true);

}

void writeContinue2(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744784774,2,data,true);
	appendBits(w304.var1744796777,2,data,true);
	appendBits(w304.var17447117780,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var17446932752,18,data,true);
	appendBits(w304.var17446940753,18,data,true);
	appendBits(w304.var17446944754,18,data,true);
	appendBits(w304.var17446952755,3,data,true);
	appendBits(w304.var17446960756,3,data,true);
	appendBits(w304.var17446972757,3,data,true);

}

void writeContinue4(W304&w304,QByteArray& data){
// head
	appendBits(w304.var1744784775,2,data,true);
	appendBits(w304.var1744796778,2,data,true);
	appendBits(w304.var17447117781,5,data,true);

// body
 
	int index =0; 
	 int count = 0;	appendBits(w304.var1744743766,0,data,true);
	appendBits(w304.var17446978758,16,data,true);
	appendBits(w304.var17446988759,10,data,true);
	appendBits(w304.var17446993760,1,data,true);
	appendBits(w304.var174472761,1,data,true);
	appendBits(w304.var1744710762,2,data,true);
	appendBits(w304.var1744718763,15,data,true);
	appendBits(w304.var1744727764,2,data,true);
	appendBits(w304.var1744734765,2,data,true);
	appendBits(w304.var1744743766,18,data,true);

}
QString checkEncodeSeqNumber(W304 &w304)
{
	QString seqNum;
	int flag =0;
 
	int index =0; 
	 int count = 0;
	flag = 0;
	flag+=w304.var17446767732!=0;
	flag+=w304.var17446773733!=0;
	flag+=w304.var17446774734!=0;
	flag+=w304.var17446785735!=0;
	flag+=w304.var17446798736!=0;
	flag+=w304.var17446807737!=0;
	flag+=w304.var17446815738!=0;
	flag+=w304.var17446818739!=0;
	if(flag != 0){seqNum+="3";}
	flag = 0;
	flag+=w304.var17446832740!=0;
	flag+=w304.var17446840741!=0;
	flag+=w304.var17446848742!=22928799;
	flag+=w304.var17446848743!=0;
	if(flag != 0){seqNum+="11";}
	flag = 0;
	flag+=w304.var17446920751!=0;
	flag+=w304.var17446864744!=0;
	flag+=w304.var17446864745!=0;
	flag+=w304.var17446880746!=0;
	flag+=w304.var17446880747!=0;
	flag+=w304.var17446897748!=0;
	flag+=w304.var17446903749!=0;
	flag+=w304.var17446912750!=0;
	flag+=w304.var17446920751!=0;
	if(flag != 0){seqNum+="21";}
	flag = 0;
	flag+=w304.var17446932752!=0;
	flag+=w304.var17446940753!=0;
	flag+=w304.var17446944754!=0;
	flag+=w304.var17446952755!=0;
	flag+=w304.var17446960756!=0;
	flag+=w304.var17446972757!=0;
	if(flag != 0){seqNum+="22";}
	flag = 0;
	flag+=w304.var1744743766!=0;
	flag+=w304.var17446978758!=0;
	flag+=w304.var17446988759!=0;
	flag+=w304.var17446993760!=0;
	flag+=w304.var174472761!=0;
	flag+=w304.var1744710762!=0;
	flag+=w304.var1744718763!=0;
	flag+=w304.var1744727764!=0;
	flag+=w304.var1744734765!=0;
	flag+=w304.var1744743766!=0;
	if(flag != 0){seqNum+="24";}
	 return seqNum;}

void VerifyField(W304 &w304)
{
	int flag = 0; 
}
void updateFieldValue(W304 &w304)
{

	//bc1_备用位数为0,数据放到头hc1_**字段中
	 w304.var1744784773=w304.var17446920751;

	//bc4_备用位数为0,数据放到头hc4_**字段中
	 w304.var1744784775=w304.var1744743766;
}

void updateGroupFlag(W304 &w304)
{
}
void encodeMsg(QByteArray& data, W304 &w304){

	QString strSeqNum=checkEncodeSeqNumber(w304);
	QString temSeqNum;
	 temSeqNum="311212224";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_1
		setVerify_1(w304,data);  
		return;
	}
	 temSeqNum="3112122";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_2
		setVerify_2(w304,data);  
		return;
	}
	 temSeqNum="3112124";
	if(temSeqNum.contains(strSeqNum)){
	//Seq_3
		setVerify_3(w304,data);  
		return;
	}
	writeSeq_1(w304,data);
}


